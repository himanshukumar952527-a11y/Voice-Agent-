console.log("VAD.JS LOADED SUCCESSFULLY");
const prev = localStorage.getItem("lastUnload");
if (prev) console.log("[DEBUG] Page was reloaded. Last unload at:", prev);
window.addEventListener("pagehide", () =>
    localStorage.setItem("lastUnload", new Date().toISOString()));

// =====================================================
// ABC CLOTHING - REAL-TIME DUPLEX VOICE AGENT
//
// Browser microphone
//       ↓
// Silero VAD (+ browser echo cancellation / noise suppression)
//       ↓
// PCM16 / 16 kHz / mono  (1024-byte frames)
//       ↓
// FastAPI /ws/voice
//       ↓
// Gnani Prisma STT → Agno LLM → Gnani Timbre TTS
//       ↓
// PCM16 / 48 kHz
//       ↓
// window.assistantPlayer  (tts_playback.js)
//
// Barge-in:
// User speaks while assistant is active
//       ↓
// Silero speech probability >= 0.70 for 5 frames (~160 ms)
//       ↓
// Flush browser audio + send {"type":"interrupt"} to backend
//
// Needs from app.js : API_BASE_URL, SESSION_ID, USER_ID, isMuted,
//                     transcriptOutput, setCallActive()
// Needs from tts_playback.js : window.assistantPlayer
// =====================================================


// =====================================================
// 1. CONFIGURATION
// =====================================================

// const WS_BASE_URL = (
//     typeof API_BASE_URL !== "undefined"
//         ? API_BASE_URL
//         : "http://127.0.0.1:8000"
// ).replace(/^http/, "ws");

// const VOICE_WS_URL = `${WS_BASE_URL}/ws/voice`;


const WS_BASE_URL = window.location.origin.replace(/^http/, "ws");

const VOICE_WS_URL = `${WS_BASE_URL}/ws/voice`;

// Barge-in tuning. One Silero frame = 512 samples @ 16 kHz = 32 ms.
// If the assistant interrupts itself (echo), raise the probability.
const BARGE_IN_SPEECH_PROB = 0.70;
const BARGE_IN_FRAMES = 5;

// Backend expects exactly 512 samples x 2 bytes = 1024 bytes per frame.
const FRAME_SAMPLES = 512;
const FRAME_BYTES = FRAME_SAMPLES * 2;

// Must match the <script> tags in index.html.
const VAD_BASE_ASSET_PATH =
    "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.31/dist/";

const ONNX_WASM_BASE_PATH =
    "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/";


// =====================================================
// 2. DOM ELEMENTS
// =====================================================

const startButton = document.getElementById("start-vad");
const stopButton = document.getElementById("stop-vad");
const vadStatusText = document.getElementById("vad-status");


// =====================================================
// 3. GLOBAL STATE
// =====================================================

let myVAD = null;
let voiceSocket = null;

let callStarting = false;
let callStopping = false;
let callActive = false;

// True from "turn_start" until the reply has fully played, or until the
// customer barges in. It also gates incoming audio: while it is false,
// audio from an old / interrupted turn is dropped.
let assistantTurnActive = false;
let assistantAudioStarted = false;
let assistantComplete = false;

let interruptRequested = false;

let consecutiveSpeechFrames = 0;

let currentTurnId = null;
let currentAssistantText = "";

let lastTranscriptTime = 0;


// =====================================================
// 4. HELPERS
// =====================================================

function setVADStatus(message) {

    if (vadStatusText) {
        vadStatusText.textContent = message;
    }
}


function dispatchVoiceEvent(name, detail = {}) {

    window.dispatchEvent(
        new CustomEvent(name, { detail })
    );
}


function isMicrophoneMuted() {

    return typeof isMuted !== "undefined" && isMuted;
}


function isSocketOpen() {

    return (
        voiceSocket &&
        voiceSocket.readyState === WebSocket.OPEN
    );
}


function sendControl(payload) {

    if (isSocketOpen()) {
        voiceSocket.send(JSON.stringify(payload));
    }
}


function scrollTranscriptToBottom() {

    if (typeof transcriptOutput === "undefined" || !transcriptOutput) {
        return;
    }

    // <pre id="transcript-output"> sits inside the scrolling container.
    const container = transcriptOutput.parentElement;

    if (container) {
        container.scrollTop = container.scrollHeight;
    }
}


function resetTurnState() {

    assistantTurnActive = false;
    assistantAudioStarted = false;
    assistantComplete = false;

    interruptRequested = false;

    consecutiveSpeechFrames = 0;

    currentAssistantText = "";
}


// =====================================================
// 5. WEBSOCKET CONNECTION
// =====================================================

function connectCallSocket() {

    return new Promise((resolve, reject) => {

        if (isSocketOpen()) {
            resolve();
            return;
        }

        const sessionId =
            typeof SESSION_ID !== "undefined"
                ? SESSION_ID
                : "abc-demo-customer-001";

        const userId =
            typeof USER_ID !== "undefined"
                ? USER_ID
                : "demo-customer";

        const url =
            `${VOICE_WS_URL}` +
            `?session_id=${encodeURIComponent(sessionId)}` +
            `&user_id=${encodeURIComponent(userId)}`;

        console.log("[WS] Connecting:", url);

        const socket = new WebSocket(url);

        socket.binaryType = "arraybuffer";

        voiceSocket = socket;

        socket.onopen = () => {

            console.log("[WS] Connected");

            setVADStatus("Connected. Listening...");

            resolve();
        };

        socket.onmessage = handleSocketMessage;

        socket.onerror = (error) => {

            console.error("[WS] Error:", error);

            setVADStatus("WebSocket connection error");

            if (!callActive) {
                reject(new Error("Voice WebSocket connection failed"));
            }
        };

        socket.onclose = (event) => {

            console.log("[WS] Closed:", event.code, event.reason);

            if (voiceSocket === socket && callActive && !callStopping) {

                setVADStatus("Connection lost");

                stopVoicePipeline(false);
            }
        };
    });
}


// =====================================================
// 6. SERVER MESSAGE HANDLER
// =====================================================

function handleAudioChunk(buffer) {

    // Audio is only valid inside an active, non-interrupted turn.
    // After barge-in, chunks already in flight are dropped here.
    if (!assistantTurnActive) {
        return;
    }

    if (!window.assistantPlayer) {
        console.error("[TTS] assistantPlayer not found");
        return;
    }

    if (!assistantAudioStarted) {

        assistantAudioStarted = true;

        if (lastTranscriptTime) {
            console.log(
                "[LATENCY] transcript -> first audio:",
                Math.round(performance.now() - lastTranscriptTime),
                "ms"
            );
        }

        setVADStatus("Assistant speaking...");

        dispatchVoiceEvent("assistant-speaking");
    }

    try {

        window.assistantPlayer.enqueue(buffer);

    } catch (error) {

        console.error("[TTS] Playback error:", error);

        setVADStatus("Audio playback error");
    }
}


function handleSocketMessage(event) {

    // Binary = assistant TTS audio.
    if (event.data instanceof ArrayBuffer) {
        handleAudioChunk(event.data);
        return;
    }

    let data;

    try {

        data = JSON.parse(event.data);

    } catch (error) {

        console.error("[WS] Invalid JSON:", event.data);

        return;
    }

    switch (data.type) {

        case "connected":

            console.log("[SERVER] STT connected:", data);

            setVADStatus("Connected. Listening...");

            break;


        // Gnani STT is processing the customer's speech.
        // This is NOT the assistant thinking, so no status change.
        case "processing":

            break;


        case "transcript": {

            const text = (data.text || "").trim();

            if (!text || isMicrophoneMuted()) {
                break;
            }

            console.log("[STT] Transcript:", text);

            lastTranscriptTime = performance.now();

            if (typeof transcriptOutput !== "undefined" &&
                transcriptOutput) {

                transcriptOutput.textContent += `You: ${text}\n`;

                scrollTranscriptToBottom();
            }

            dispatchVoiceEvent("voice-transcript", {
                text: text,
                language: data.language,
                latency: data.latency
            });

            break;
        }


        case "turn_start":

            console.log("[TURN] Started:", data.turn_id);

            // Fresh turn: clear everything, then accept audio again.
            resetTurnState();

            currentTurnId = data.turn_id;

            assistantTurnActive = true;

            setVADStatus("Assistant is responding...");

            dispatchVoiceEvent("assistant-thinking");

            break;


        case "text_delta":

            if (!assistantTurnActive || !data.text) {
                break;
            }

            // Server sends trimmed chunks, so join them with a space.
            currentAssistantText +=
                (currentAssistantText ? " " : "") + data.text;

            dispatchVoiceEvent("assistant-text-delta", {
                text: data.text
            });

            break;


        case "complete":

            // Ignore a stale "complete" from an interrupted/old turn.
            if (!assistantTurnActive || data.turn_id !== currentTurnId) {
                break;
            }

            console.log("[TURN] Generation complete:", data.metrics);

            assistantComplete = true;

            if (data.text) {
                currentAssistantText = data.text;
            }

            finishTurnIfReady();

            break;


        // Server acknowledgement. The browser already flushed audio and
        // reset its state the moment barge-in fired, so nothing to do.
        case "interrupted":

            console.log("[TURN] Interrupt acknowledged by server");

            break;


        case "error":

            console.error("[SERVER] Error:", data.message);

            setVADStatus("Error: " + data.message);

            dispatchVoiceEvent("voice-error", {
                message: data.message
            });

            break;


        default:

            console.log("[SERVER] Unknown event:", data);

            break;
    }
}


// =====================================================
// 7. TURN COMPLETION
// =====================================================

// A turn ends only when the server finished generating AND the speakers
// have drained. (Audio can underrun between TTS chunks while the server
// is still working, so "drained" alone is not enough.)
function finishTurnIfReady() {

    if (!assistantTurnActive || !assistantComplete) {
        return;
    }

    if (
        window.assistantPlayer &&
        window.assistantPlayer.isPlaying()
    ) {
        return;
    }

    console.log("[TURN] Audio playback drained");

    const text = currentAssistantText.trim();

    // Fired only after the customer actually heard the reply.
    if (text) {

        dispatchVoiceEvent("assistant-response", { text });

        scrollTranscriptToBottom();
    }

    resetTurnState();

    if (callActive && !isMicrophoneMuted()) {
        setVADStatus("Listening...");
    }

    dispatchVoiceEvent("assistant-speaking-ended");
}


// =====================================================
// 8. MICROPHONE AUDIO → PCM16
// =====================================================

function convertFloat32ToPCM16(audioFrame) {

    if (!audioFrame || audioFrame.length !== FRAME_SAMPLES) {

        console.warn(
            "[MIC] Unexpected frame size:",
            audioFrame ? audioFrame.length : 0
        );

        return null;
    }

    const pcmBuffer = new ArrayBuffer(FRAME_BYTES);

    const view = new DataView(pcmBuffer);

    for (let i = 0; i < FRAME_SAMPLES; i++) {

        const sample = Math.max(-1, Math.min(1, audioFrame[i]));

        const pcmValue =
            sample < 0
                ? sample * 32768
                : sample * 32767;

        view.setInt16(i * 2, pcmValue, true);
    }

    return pcmBuffer;
}


// =====================================================
// 9. BARGE-IN DETECTION
// =====================================================

function triggerBargeIn() {

    if (!assistantTurnActive || interruptRequested) {
        return;
    }

    const startedAt = performance.now();

    console.log("[BARGE-IN] User interrupted assistant");

    // 1) Silence the speakers right now (local, no network).
    if (window.assistantPlayer) {
        window.assistantPlayer.flush();
    }

    // 2) Close the turn locally: in-flight audio and late text/complete
    //    events from the old turn are ignored from here on.
    resetTurnState();

    interruptRequested = true;

    // 3) Tell the backend to cancel LLM + TTS, on the same socket.
    sendControl({ type: "interrupt" });

    setVADStatus("You are speaking...");

    dispatchVoiceEvent("assistant-interrupted");

    console.log(
        "[BARGE-IN] Audio flushed in",
        (performance.now() - startedAt).toFixed(1),
        "ms"
    );
}


function checkBargeIn(speechProbability) {

    // Only while an assistant turn is active.
    if (!assistantTurnActive || interruptRequested) {
        consecutiveSpeechFrames = 0;
        return;
    }

    if (speechProbability >= BARGE_IN_SPEECH_PROB) {

        consecutiveSpeechFrames++;

    } else {

        // Leaky counter: one weak frame doesn't reset real speech.
        consecutiveSpeechFrames = Math.max(
            0,
            consecutiveSpeechFrames - 1
        );
    }

    if (consecutiveSpeechFrames >= BARGE_IN_FRAMES) {

        triggerBargeIn();
    }
}


// =====================================================
// 10. SILERO VAD FRAME CALLBACK
// =====================================================

function handleProcessedFrame(probabilities, audioFrame) {

    if (!callActive || callStopping) {
        return;
    }

    if (isMicrophoneMuted()) {
        consecutiveSpeechFrames = 0;
        return;
    }

    if (!isSocketOpen()) {
        return;
    }

    const speechProbability =
        typeof probabilities?.isSpeech === "number"
            ? probabilities.isSpeech
            : 0;

    checkBargeIn(speechProbability);

    const pcmFrame = convertFloat32ToPCM16(audioFrame);

    if (!pcmFrame) {
        return;
    }

    // Every frame goes to Prisma STT, so nothing the customer says
    // during the first ~160 ms of an interruption is lost.
    try {

        voiceSocket.send(pcmFrame);

    } catch (error) {

        console.error("[MIC] Frame send failed:", error);
    }
}


// =====================================================
// 11. START VOICE PIPELINE
// =====================================================

async function startVoicePipeline() {

    if (callStarting || callActive) {
        return;
    }

    callStarting = true;
    callStopping = false;

    try {

        console.log("[CALL] Starting voice pipeline");

        setVADStatus("Initializing audio...");

        if (!window.assistantPlayer) {
            throw new Error("assistantPlayer is not loaded");
        }

        // Must run inside the Start Call click (browser autoplay rule).
        await window.assistantPlayer.init();

        window.assistantPlayer.flush();

        window.assistantPlayer.onDrain(finishTurnIfReady);

        resetTurnState();

        lastTranscriptTime = 0;

        // Connect the socket before the microphone starts.
        await connectCallSocket();

        if (!window.vad || !window.vad.MicVAD) {
            throw new Error("Silero VAD library is not loaded");
        }

        setVADStatus("Initializing Silero VAD...");

        myVAD = await window.vad.MicVAD.new({

            model: "v5",

            baseAssetPath: VAD_BASE_ASSET_PATH,

            onnxWASMBasePath: ONNX_WASM_BASE_PATH,

            // Browser echo cancellation is what keeps the assistant's own
            // voice from triggering barge-in. Sample rate is resampled
            // to 16 kHz by vad-web itself.
            getStream: async () => {

                return await navigator.mediaDevices.getUserMedia({

                    audio: {
                        channelCount: 1,
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true
                    }

                });
            },

            // These only affect onSpeechStart/onSpeechEnd (UI hints).
            // Barge-in uses the raw per-frame probability instead.
            positiveSpeechThreshold: 0.7,

            negativeSpeechThreshold: 0.5,

            redemptionMs: 500,

            preSpeechPadMs: 300,

            minSpeechMs: 150,

            onSpeechStart: () => {

                console.log("[VAD] Speech started");

                if (!assistantTurnActive) {
                    setVADStatus("Listening to you...");
                }
            },

            onSpeechEnd: () => {

                console.log("[VAD] Speech ended");

                if (!assistantTurnActive) {
                    setVADStatus("Processing...");
                }
            },

            onFrameProcessed: (probabilities, audioFrame) => {

                handleProcessedFrame(probabilities, audioFrame);
            }
        });

        await myVAD.start();

        callActive = true;

        callStarting = false;

        setVADStatus("Listening...");

        console.log("[CALL] Voice pipeline started");

        dispatchVoiceEvent("voice-call-started");

        if (typeof setCallActive === "function") {
            setCallActive(true);
        }

    } catch (error) {

        console.error("[CALL] Start failed:", error);

        setVADStatus("Start failed: " + error.message);

        callStarting = false;

        callActive = false;

        if (myVAD) {

            try {
                await myVAD.destroy();
            } catch (e) {
                console.error("[VAD] Cleanup error:", e);
            }

            myVAD = null;
        }

        if (voiceSocket) {

            try {
                voiceSocket.close();
            } catch (e) {
                console.error("[WS] Cleanup error:", e);
            }

            voiceSocket = null;
        }

        if (typeof setCallActive === "function") {
            setCallActive(false);
        }
    }
}


// =====================================================
// 12. STOP VOICE PIPELINE
// =====================================================

async function stopVoicePipeline(sendStopMessage = true) {

    if (callStopping) {
        return;
    }

    callStopping = true;

    console.log("[CALL] Stopping voice pipeline");

    callActive = false;

    // Stop local audio immediately.
    if (window.assistantPlayer) {
        window.assistantPlayer.flush();
    }

    resetTurnState();

    // Stop Silero and release the microphone.
    if (myVAD) {

        try {
            await myVAD.destroy();
        } catch (error) {
            console.error("[VAD] Destroy error:", error);
        }

        myVAD = null;
    }

    // Tell the backend the call is over, then close.
    if (voiceSocket) {

        const socket = voiceSocket;

        voiceSocket = null;

        try {

            if (
                sendStopMessage &&
                socket.readyState === WebSocket.OPEN
            ) {
                socket.send(JSON.stringify({ type: "stop" }));
            }

            socket.close();

        } catch (error) {

            console.error("[WS] Stop error:", error);
        }
    }

    callStarting = false;
    callStopping = false;

    setVADStatus("Call ended");

    dispatchVoiceEvent("voice-call-ended");

    if (typeof setCallActive === "function") {
        setCallActive(false);
    }

    console.log("[CALL] Voice pipeline stopped");
}


// =====================================================
// 13. BUTTON EVENTS
// =====================================================

if (startButton) {

    startButton.addEventListener("click", () => {

        startVoicePipeline();
    });
}


if (stopButton) {

    stopButton.addEventListener("click", () => {

        stopVoicePipeline();
    });
}


// =====================================================
// 14. PAGE CLEANUP
// =====================================================

window.addEventListener("beforeunload", () => {

    callActive = false;

    if (window.assistantPlayer) {
        window.assistantPlayer.flush();
    }

    if (voiceSocket) {

        try {
            voiceSocket.close();
        } catch (error) {
            console.error(error);
        }
    }
});


console.log("VAD voice pipeline initialized.");