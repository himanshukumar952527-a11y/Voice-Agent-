console.log("TTS_PLAYBACK.JS LOADED SUCCESSFULLY");

// ========================================
// Audio playback engine.
//
// The call websocket lives in vad.js. This file only:
//   * plays PCM16 / 48 kHz chunks gap-free (scheduled on the Web Audio clock)
//   * flushes everything instantly on barge-in
//   * powers the standalone "Test TTS" button
//
// Exposed as window.assistantPlayer.
// ========================================

const SAMPLE_RATE = 48000;

// Small safety lead so the first chunk of a turn doesn't glitch.
const START_LEAD_SECONDS = 0.04;

const TTS_WS_URL = "ws://127.0.0.1:8000/voice/tts";

const TEST_TEXT =
    "Namaste! ABC Clothing mein aapka swagat hai. Main aapki kya sahayata kar sakti hoon?";

const testButton = document.getElementById("test-tts");
const ttsStatusText = document.getElementById("tts-status");


function setTTSStatus(message) {

    if (ttsStatusText) {
        ttsStatusText.textContent = message;
    }
}


window.assistantPlayer = (() => {

    let ctx = null;
    let nextTime = 0;
    let drainHandler = null;

    const sources = new Set();

    // Must be called from a user gesture (Start Call click).
    async function init() {

        if (!ctx || ctx.state === "closed") {

            ctx = new AudioContext({
                sampleRate: SAMPLE_RATE,
                latencyHint: "interactive"
            });
        }

        if (ctx.state !== "running") {
            await ctx.resume();
        }

        nextTime = ctx.currentTime;
    }

    function enqueue(arrayBuffer) {

        if (!ctx || ctx.state !== "running") {
            throw new Error("AudioContext is not running");
        }

        let buffer = arrayBuffer;

        if (buffer.byteLength % 2 !== 0) {
            buffer = buffer.slice(0, buffer.byteLength - 1);
        }

        if (buffer.byteLength < 2) {
            return;
        }

        const pcm = new Int16Array(buffer);
        const floats = new Float32Array(pcm.length);

        for (let i = 0; i < pcm.length; i++) {
            floats[i] = pcm[i] / 32768;
        }

        const audioBuffer = ctx.createBuffer(1, floats.length, SAMPLE_RATE);

        audioBuffer.copyToChannel(floats, 0);

        const source = ctx.createBufferSource();

        source.buffer = audioBuffer;
        source.connect(ctx.destination);

        // Chain onto the previous chunk; after an underrun restart
        // with a small lead.
        const startAt = nextTime > ctx.currentTime
            ? nextTime
            : ctx.currentTime + START_LEAD_SECONDS;

        source.start(startAt);

        nextTime = startAt + audioBuffer.duration;

        sources.add(source);

        source.onended = () => {

            sources.delete(source);

            try { source.disconnect(); } catch (e) { /* ignore */ }

            if (sources.size === 0 && drainHandler) {
                drainHandler();
            }
        };
    }

    // Instant barge-in: stop every scheduled/playing chunk right now.
    function flush() {

        const wasPlaying = sources.size > 0;

        for (const source of sources) {

            source.onended = null;

            try { source.stop(); } catch (e) { /* already stopped */ }
            try { source.disconnect(); } catch (e) { /* ignore */ }
        }

        sources.clear();

        if (ctx) {
            nextTime = ctx.currentTime;
        }

        return wasPlaying;
    }

    return {
        init,
        enqueue,
        flush,
        isPlaying: () => sources.size > 0,
        onDrain: (handler) => { drainHandler = handler; }
    };
})();


// ========================================
// Standalone TTS test button
// ========================================

async function startTTS() {

    if (!testButton) return;

    testButton.disabled = true;

    try {

        await window.assistantPlayer.init();

        window.assistantPlayer.flush();

        setTTSStatus("Generating voice...");

        const socket = new WebSocket(TTS_WS_URL);

        socket.binaryType = "arraybuffer";

        socket.onopen = () => {
            socket.send(JSON.stringify({ text: TEST_TEXT }));
        };

        socket.onmessage = (event) => {

            if (event.data instanceof ArrayBuffer) {

                try {
                    window.assistantPlayer.enqueue(event.data);
                    setTTSStatus("Playing AI voice...");
                } catch (error) {
                    console.error("TTS playback error:", error);
                    setTTSStatus("Audio playback error");
                }

                return;
            }

            try {

                const data = JSON.parse(event.data);

                if (data.type === "complete") {
                    setTTSStatus("TTS generation completed!");
                } else if (data.type === "error") {
                    setTTSStatus("Error: " + data.message);
                }

            } catch (error) {
                console.error("Invalid TTS message:", event.data);
            }
        };

        socket.onerror = () => setTTSStatus("WebSocket connection error");

        socket.onclose = () => { testButton.disabled = false; };

    } catch (error) {

        console.error("TTS initialization error:", error);

        setTTSStatus("Error: " + error.message);

        testButton.disabled = false;
    }
}

if (testButton) {
    testButton.addEventListener("click", startTTS);
}

window.addEventListener("beforeunload", () => {
    window.assistantPlayer.flush();
});

console.log("TTS playback engine initialized.");