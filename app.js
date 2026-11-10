// =========================================
// ABC CLOTHING - AI VOICE SUPPORT
// =========================================

const API_BASE_URL = "http://127.0.0.1:8000";
const HEALTH_URL = `${API_BASE_URL}/health`;


// =========================================
// SESSION CONFIGURATION
// =========================================

let SESSION_ID = "abc-demo-customer-001";
const USER_ID = "demo-customer";


// =========================================
// HTML ELEMENTS
// =========================================

const connectionLabel = document.getElementById("connection-status");
const statusPill = document.querySelector(".status-pill");

const sessionIdEl = document.getElementById("session-id");

const agentStatusEl = document.getElementById("agent-status");
const agentDescriptionEl = document.getElementById("agent-status-description");

const detectedIntentEl = document.getElementById("detected-intent");

const memorySessionEl = document.getElementById("memory-session");
const memoryNoteEl = document.getElementById("memory-note");

const newSessionButton = document.getElementById("new-session-button");

const callTimerEl = document.getElementById("call-timer");
const callStatusEl = document.getElementById("vad-status");

const startCallButton = document.getElementById("start-vad");
const endCallButton = document.getElementById("stop-vad");

const muteButton = document.getElementById("mute-button");
const muteIcon = document.getElementById("mute-icon");

const visualizer = document.getElementById("voice-visualizer");
const transcriptOutput = document.getElementById("transcript-output");


// =========================================
// SESSION STATE
// =========================================

let callStartTime = null;
let callTimerInterval = null;
let isCallActive = false;
let isMuted = false;


// =========================================
// SESSION UI
// =========================================

function updateSessionUI() {

    if (sessionIdEl) {
        sessionIdEl.textContent = SESSION_ID;
    }

    if (memorySessionEl) {
        memorySessionEl.textContent = isCallActive
            ? "Active Call Session"
            : "No active call";
    }

    if (memoryNoteEl) {
        memoryNoteEl.textContent = isCallActive
            ? "Conversation context is maintained using session history."
            : "Start a call to begin a new customer session.";
    }

}

updateSessionUI();


// =========================================
// CONNECTION STATUS
// =========================================

function updateConnectionStatus(connected) {

    if (!connectionLabel) return;

    connectionLabel.textContent = connected
        ? "Backend connected"
        : "Backend offline";

    statusPill?.classList.toggle("connected", connected);

}


// =========================================
// AGENT STATUS
// =========================================

function updateAgentStatus(status, description) {

    if (agentStatusEl) {
        agentStatusEl.textContent = status;
    }

    if (agentDescriptionEl) {
        agentDescriptionEl.textContent = description;
    }

}


// =========================================
// CALL STATUS
// =========================================

function updateCallStatus(status) {

    if (callStatusEl) {
        callStatusEl.textContent = status;
    }

}


// =========================================
// CALL TIMER
// =========================================

function formatCallTime(seconds) {

    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;

    return (
        String(minutes).padStart(2, "0") +
        ":" +
        String(remainingSeconds).padStart(2, "0")
    );

}


function startCallTimer() {

    stopCallTimer();

    callStartTime = Date.now();

    if (callTimerEl) {
        callTimerEl.textContent = "00:00";
    }

    callTimerInterval = setInterval(() => {

        if (!callStartTime || !callTimerEl) return;

        const elapsedSeconds = Math.floor(
            (Date.now() - callStartTime) / 1000
        );

        callTimerEl.textContent = formatCallTime(elapsedSeconds);

    }, 1000);

}


function stopCallTimer() {

    if (callTimerInterval) {
        clearInterval(callTimerInterval);
        callTimerInterval = null;
    }

    callStartTime = null;

}


// =========================================
// CALL UI
// =========================================

function setCallActive(active) {

    isCallActive = active;

    if (startCallButton) {
        startCallButton.disabled = active;
    }

    if (endCallButton) {
        endCallButton.disabled = !active;
    }

    if (muteButton) {
        muteButton.disabled = !active;
    }

    if (visualizer) {
        visualizer.classList.toggle("active", active);
    }

    if (active) {

        startCallTimer();

        updateAgentStatus(
            "On Call",
            "Shivani is ready to assist the customer."
        );

        if (memorySessionEl) {
            memorySessionEl.textContent = "Active Call Session";
        }

        if (memoryNoteEl) {
            memoryNoteEl.textContent =
                "Customer conversation is being maintained.";
        }

    } else {

        stopCallTimer();

        updateAgentStatus(
            "Ready",
            "Waiting for an incoming call."
        );

        if (memorySessionEl) {
            memorySessionEl.textContent = "No active call";
        }

        if (memoryNoteEl) {
            memoryNoteEl.textContent =
                "Start a call to begin a new customer session.";
        }

        isMuted = false;
        updateMuteUI();

    }

}


// =========================================
// MUTE UI
// =========================================

function updateMuteUI() {

    if (!muteButton || !muteIcon) return;

    muteIcon.textContent = isMuted ? "🔇" : "🎙";

    muteButton.classList.toggle("muted", isMuted);

    muteButton.setAttribute(
        "aria-label",
        isMuted ? "Unmute microphone" : "Mute microphone"
    );

}


// =========================================
// MUTE BUTTON
// =========================================

// UI state only. Actual microphone muting
// needs to be connected with the VAD pipeline.

muteButton?.addEventListener("click", () => {

    if (!isCallActive) return;

    isMuted = !isMuted;

    updateMuteUI();

    updateCallStatus(
        isMuted
            ? "Microphone muted"
            : "Voice pipeline running"
    );

});


// =========================================
// INTENT DETECTION
// =========================================

function detectIntent(message) {

    const text = message.toLowerCase();

    const has = (words) => {

        return words.some(word => {

            return new RegExp(
                `(^|\\W)${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=$|\\W)`,
                "i"
            ).test(text);

        });

    };

    if (has(["order", "delivery", "track", "tracking"])) {
        return "Order Inquiry";
    }

    if (has(["price", "cost", "rate"])) {
        return "Price Inquiry";
    }

    if (has(["bulk", "wholesale", "quantity"])) {
        return "Bulk Order";
    }

    if (has(["return", "refund", "exchange"])) {
        return "Return / Refund";
    }

    if (has(["hello", "hi", "hey", "namaste"])) {
        return "Greeting";
    }

    return "General Inquiry";

}


// =========================================
// HANDLE VOICE TRANSCRIPT
// =========================================

function updateDetectedIntent(message) {

    if (!detectedIntentEl || !message) return;

    detectedIntentEl.textContent = detectIntent(message);

}


// =========================================
// NEW SESSION
// =========================================

function createNewSession() {

    if (isCallActive) {

        alert("Please end the current call before starting a new session.");

        return;

    }

    SESSION_ID = `abc-customer-${Date.now()}`;

    updateSessionUI();

    if (detectedIntentEl) {
        detectedIntentEl.textContent = "General Inquiry";
    }

    if (transcriptOutput) {
        transcriptOutput.textContent =
            "Your conversation will appear here when the call starts...";
    }

    updateAgentStatus(
        "Ready",
        "Waiting for an incoming call."
    );

    updateCallStatus("Ready to connect");

    if (callTimerEl) {
        callTimerEl.textContent = "00:00";
    }

    console.log("[SESSION] New session created:", SESSION_ID);

}


// =========================================
// NEW SESSION BUTTON
// =========================================

newSessionButton?.addEventListener(
    "click",
    createNewSession
);


// =========================================
// BACKEND HEALTH CHECK
// =========================================

async function checkBackend() {

    try {

        const response = await fetch(HEALTH_URL, {
            method: "GET",
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error("Backend unavailable");
        }

        updateConnectionStatus(true);

    } catch (error) {

        updateConnectionStatus(false);

    }

}


// =========================================
// VOICE EVENT INTEGRATION
// =========================================

// Called when VAD successfully starts.

window.addEventListener("voice-call-started", () => {

    setCallActive(true);

    updateCallStatus("Voice pipeline running");

});


// Called when VAD stops.

window.addEventListener("voice-call-ended", () => {

    setCallActive(false);

    updateCallStatus("Call ended");

});


// Called when STT receives a transcript.

window.addEventListener("voice-transcript", (event) => {

    const transcript = event.detail?.text;

    if (!transcript) return;

    updateDetectedIntent(transcript);

    updateAgentStatus(
        "Listening",
        "Customer speech detected."
    );

});


// Called when assistant begins responding.

window.addEventListener("assistant-thinking", () => {

    updateAgentStatus(
        "Thinking",
        "Generating an AI response."
    );

});


// Called when assistant response starts.

window.addEventListener("assistant-speaking", () => {

    updateAgentStatus(
        "Speaking",
        "Shivani is responding to the customer."
    );

});


// Called the moment the customer barges in.

window.addEventListener("assistant-interrupted", () => {

    updateAgentStatus(
        "Listening",
        "Customer interrupted. Shivani stopped speaking."
    );

});


// Called when the full reply has been spoken to the end.

window.addEventListener("assistant-response", (event) => {

    const text = event.detail?.text;

    if (transcriptOutput && text) {
        transcriptOutput.textContent += "Shivani: " + text + "\n";
    }

    updateAgentStatus(
        "On Call",
        "Shivani is ready to assist the customer."
    );

});


// =========================================
// INITIALIZE APPLICATION
// =========================================

checkBackend();

setInterval(checkBackend, 15000);

updateMuteUI();

console.log("[APP] ABC Clothing Voice Dashboard initialized.");