

"""
ABC Clothing - real-time duplex voice agent (FastAPI)

One WebSocket per call:  ws://127.0.0.1:8000/ws/voice?session_id=..&user_id=..

Browser -> server
    binary : 1024-byte PCM16 / 16 kHz mono mic frames  (forwarded to Gnani STT)
    json   : {"type": "interrupt"}   barge-in, cancel the current turn NOW
             {"type": "stop"}        end of call

Server -> browser
    json   : connected | processing | transcript | turn_start | text_delta |
             interrupted | complete | error
    binary : PCM16 / 48 kHz TTS audio for the current turn

Pipeline for every turn (all stages run concurrently):

    LLM tokens --> clause chunker --> text_q --> dispatcher --> [TTS fetch tasks, parallel]
                                                    order_q --> sender --> browser

Install:  pip install fastapi "uvicorn[standard]" python-dotenv
"""

import os
import re
import json
import time
import asyncio
import threading
import traceback

from pathlib import Path
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from gnani.stt import (
    GnaniSTTStreamClient,
    StreamTranscriptEvent,
    StreamConnectedEvent,
    StreamProcessingEvent,
    StreamErrorEvent,
)

from gnani.tts import (
    GnaniTTSRealtimeClient,
    AudioConfig,
)

from agno.agent import Agent, RunEvent
from agno.models.groq import Groq
from agno.db.sqlite import SqliteDb


# ==================================================
# 1. CONFIGURATION
# ==================================================

load_dotenv()

ROOT = Path(__file__).resolve().parent

BUSINESS_FILE = ROOT / "business_info.txt"
DATA_DIR = ROOT.parent / "voice_agent_data"
DATA_DIR.mkdir(exist_ok=True)

DB_FILE = DATA_DIR / "abc_agent_sessions.db"
RESPONSE_FILE = DATA_DIR / "agent_responses.json"

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GNANI_API_KEY = os.getenv("GNANI_API_KEY")

GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")

APP_VERSION = "greet-1"
print(f"[APP] ABC voice agent file loaded, version {APP_VERSION}, from {Path(__file__).resolve()}")

TTS_VOICE = "Riyaa"
TTS_MODEL = "timbre-v3.0"

TTS_AUDIO_CONFIG = AudioConfig(
    sample_rate=48000,
    encoding="linear_pcm",
    num_channels=1,
    sample_width=2,
    container="wav",
)

# What Shivani says the moment the call connects (before the customer speaks).
# Each line is synthesized as its own chunk. The audio is generated once at
# server start-up and cached, so on a call the greeting starts instantly.
GREETING_CHUNKS = [
    "नमस्ते! ABC Clothing में आपका स्वागत है।",
    "This is Shivani, an AI agent for ABC Clothing.",
    "बताइए, मैं आपकी क्या मदद कर सकती हूँ?",
]
GREETING_SET = set(GREETING_CHUNKS)
TTS_CACHE = {}

# Gnani's synthesize() opens a NEW websocket per call, so we start the next
# chunk's synthesis while the previous chunk is still streaming (hides the
# connection handshake). Audio is still played strictly in order.
MAX_PARALLEL_TTS = int(os.getenv("MAX_PARALLEL_TTS", "3"))

# If Gnani sends nothing for this long, give up on that chunk instead of
# letting the whole turn hang.
TTS_STALL_TIMEOUT = float(os.getenv("TTS_STALL_TIMEOUT", "10"))

STT_CLOSE_TIMEOUT = float(os.getenv("STT_CLOSE_TIMEOUT", "2"))

# Typed-text input over the voice socket ({"type":"text","text":"..."}).
# Handy for testing the pipeline without a microphone.
ALLOW_TEXT_INPUT = os.getenv("ALLOW_TEXT_INPUT", "1") == "1"

# Gnani logs one DEBUG line per audio chunk (100+ per sentence). Hide them
# unless GNANI_DEBUG=1 so our own timing logs stay readable.
if os.getenv("GNANI_DEBUG") != "1":
    import logging

    for _name in list(logging.root.manager.loggerDict):
        if _name.startswith("gnani"):
            logging.getLogger(_name).setLevel(logging.INFO)

STT_FRAME_BYTES = 1024  # 512 samples * 2 bytes (Silero frame at 16 kHz)

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing from .env")

if not GNANI_API_KEY:
    print("WARNING: GNANI_API_KEY is missing from .env")


# ==================================================
# 2. BUSINESS INFORMATION
# ==================================================

if BUSINESS_FILE.exists():
    BUSINESS_INFO = BUSINESS_FILE.read_text(encoding="utf-8")
else:
    BUSINESS_INFO = """
ABC Clothing is a demo wholesale and retail clothing business.

Categories:
Men, Women, Kids, Winter and Ethnic clothing.

Minimum bulk order: 50 pieces.
Orders above 500 pieces require a special quotation.

Delivery: 5-7 business days, depending on location
and order quantity.

Payment: 50% advance and remaining amount before dispatch.

Damaged products must be reported within 48 hours
and are subject to verification.

Business hours: 10 AM to 7 PM.
"""


# ==================================================
# 3. AGNO AGENT
# ==================================================

agent = Agent(
    name="ABC Clothing Customer Care Agent",

    model=Groq(
        id=GROQ_MODEL,
        api_key=GROQ_API_KEY,
    ),

    instructions=[
        "You are the customer-care agent for ABC Clothing.",
        "Reply in the customer's language. If the customer speaks Hindi (even mixed with English words), reply in Hindi written in Devanagari script, but keep English brand and product words such as ABC Clothing, jeans, shirt, wholesale in English letters. Never write Hindi in Roman letters. If the customer speaks only English, reply in English.",
        "The call greeting (welcome, your name, and offer to help) has already been spoken automatically when the call connected. Never greet or introduce yourself again; answer what the customer says directly.",
        "Use a friendly and professional conversational tone.",
        "Use feminine Hindi phrasing where appropriate.",
        "Use plain text only. Do not use markdown or asterisks.",
        "Keep replies short: 2-4 short sentences. Ask at most one question.",
        "Remember details already shared in this conversation.",
        "Never invent prices, stock, tracking details, delivery times, or order status.",
        "Never claim an order or payment has been completed unless a real backend operation confirms it.",
        "If business data is unavailable, explain that clearly.",
        "If the customer's message is unclear, politely ask for clarification.",
        "Business information:\n" + BUSINESS_INFO,
    ],

    db=SqliteDb(db_file=str(DB_FILE)),
    add_history_to_context=True,
    num_history_runs=8,
    markdown=False,
)


# ==================================================
# 4. TEXT HELPERS
# ==================================================

def clean_response(text):

    if not text:
        return ""

    text = re.sub(
        r"<think>.*?</think>",
        "",
        str(text),
        flags=re.DOTALL,
    )

    return text.strip()


def get_agent_response(message, session_id, user_id):

    result = agent.run(
        message,
        session_id=session_id,
        user_id=user_id,
    )

    reply = clean_response(getattr(result, "content", None))

    if not reply:
        return "Ji, bataiye. Aapko kis cheez mein madad chahiye?"

    return reply


class ThinkStripper:
    """
    Streaming-safe removal of <think>...</think> blocks.

    Handles tags split across tokens and keeps any text that follows
    </think> inside the same token (the old code dropped it).
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self):
        self.buf = ""
        self.in_think = False

    @staticmethod
    def _partial(buf, tag):
        """Length of the longest buf suffix that is a proper prefix of tag."""
        for n in range(min(len(tag) - 1, len(buf)), 0, -1):
            if buf.endswith(tag[:n]):
                return n
        return 0

    def feed(self, token):

        self.buf += token
        out = []

        while True:

            if self.in_think:

                i = self.buf.find(self.CLOSE)

                if i == -1:
                    keep = self._partial(self.buf, self.CLOSE)
                    self.buf = self.buf[len(self.buf) - keep:]
                    break

                self.buf = self.buf[i + len(self.CLOSE):]
                self.in_think = False

            else:

                i = self.buf.find(self.OPEN)

                if i == -1:
                    keep = self._partial(self.buf, self.OPEN)
                    cut = len(self.buf) - keep
                    out.append(self.buf[:cut])
                    self.buf = self.buf[cut:]
                    break

                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.in_think = True

        return "".join(out)

    def finish(self):

        rest = "" if self.in_think else self.buf
        self.buf = ""

        return rest


class ClauseChunker:
    """
    Splits streamed LLM text into speakable chunks.

    * First chunk is small (3-7 words, or the first sentence/clause) so the
      first audio starts as early as possible.
    * Later chunks are bigger (6-14 words) so prosody stays natural and we
      don't pay a TTS connection for every couple of words.
    * Boundaries are only accepted when followed by whitespace, so "3.5"
      is never split.
    """

    FIRST_MIN, FIRST_MAX = 3, 9
    MIN, MAX = 8, 24

    BOUNDARY = re.compile(r"[.!?।]+(?=\s)|[,;:–—](?=\s)")
    WORD = re.compile(r"\S+\s")

    def __init__(self):
        self.buf = ""
        self.sent = False

    def _cut(self, end):

        chunk = self.buf[:end].strip()
        self.buf = self.buf[end:].lstrip()

        if not chunk:
            return self._next()

        self.sent = True

        return chunk

    def _next(self):

        first = not self.sent

        min_w = self.FIRST_MIN if first else self.MIN
        max_w = self.FIRST_MAX if first else self.MAX

        for m in self.BOUNDARY.finditer(self.buf):

            # Never split inside an open bracket: "(Men, Women, ya Kids)".
            before = self.buf[:m.start()]

            if before.count("(") > before.count(")"):
                continue

            n = len(self.buf[:m.end()].split())
            hard = m.group()[0] in ".!?।"

            if n >= min_w or (hard and (first or n >= 3)):
                return self._cut(m.end())

        words = list(self.WORD.finditer(self.buf))

        if len(words) >= max_w:
            return self._cut(words[max_w - 1].end())

        return None

    def add(self, text):

        self.buf += text

        out = []

        while True:
            chunk = self._next()
            if chunk is None:
                break
            out.append(chunk)

        return out

    def flush(self):

        chunk = self.buf.strip()
        self.buf = ""

        if chunk:
            self.sent = True
            return chunk

        return None


class PCMAligner:
    """
    Makes TTS audio safe for Int16Array on the browser:
      * strips a leading WAV header if the server sends one per request
      * never emits an odd number of bytes
    """

    def __init__(self):
        self.carry = b""
        self.first = True

    def feed(self, data):

        data = bytes(data)

        if self.first:
            self.first = False

            if data[:4] == b"RIFF":
                idx = data.find(b"data")

                if idx != -1 and len(data) >= idx + 8:
                    data = data[idx + 8:]
                else:
                    data = b""

        data = self.carry + data

        if len(data) % 2:
            self.carry = data[-1:]
            data = data[:-1]
        else:
            self.carry = b""

        return data


# ==================================================
# 5. CONVERSATION STORAGE
# ==================================================

_save_lock = threading.Lock()


def _write_record(message, reply, session_id, user_id, interrupted=False):

    record = {
        "timestamp": datetime.now().isoformat(),
        "session_id": session_id,
        "user_id": user_id,
        "user_message": message,
        "agent_response": reply,
        "interrupted": interrupted,
    }

    responses = []

    if RESPONSE_FILE.exists():

        try:
            with open(RESPONSE_FILE, "r", encoding="utf-8") as file:
                responses = json.load(file)

            if not isinstance(responses, list):
                responses = []

        except (json.JSONDecodeError, OSError):
            responses = []

    responses.append(record)

    tmp_file = RESPONSE_FILE.with_suffix(".json.tmp")

    with open(tmp_file, "w", encoding="utf-8") as file:
        json.dump(responses, file, indent=4, ensure_ascii=False)

    os.replace(tmp_file, RESPONSE_FILE)


def save_agent_response(message, reply, session_id, user_id, interrupted=False):

    with _save_lock:
        _write_record(message, reply, session_id, user_id, interrupted)


# ==================================================
# 6. VOICE CALL / TURN
# ==================================================

def _ms(t0, t1):
    return None if t1 is None else round((t1 - t0) * 1000)


class Turn:
    """One customer utterance -> assistant reply, as concurrent tasks."""

    def __init__(self, call, turn_id, user_text, fixed_text=None):

        self.call = call
        self.id = turn_id
        self.user_text = user_text

        # List of ready-made chunks to speak instead of calling the LLM
        # (used for the greeting).
        self.fixed_text = fixed_text

        self.generated = []
        self.audio_started = False
        self.cancelled = False
        self.finished = False

        self.t0 = time.perf_counter()
        self.t_first_token = None
        self.t_first_chunk = None
        self.t_first_audio = None

        self.text_q = asyncio.Queue()
        self.order_q = asyncio.Queue()
        self.tts_sem = asyncio.Semaphore(MAX_PARALLEL_TTS)

        self.tasks = set()
        self.main_task = None

    # ---------------- lifecycle ----------------

    @property
    def active(self):
        return not (self.cancelled or self.finished)

    def start(self):
        self.main_task = asyncio.create_task(self._run())

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def cancel(self):
        """Synchronous + instant: nothing is awaited here."""

        if self.cancelled or self.finished:
            return

        self.cancelled = True

        # If the customer never heard anything, keep their words so the
        # next utterance can be merged with them.
        if not self.audio_started:
            self.call.pending_user_text = self.user_text

        for task in list(self.tasks):
            task.cancel()

        if self.main_task:
            self.main_task.cancel()

    async def wait(self, timeout=3.0):
        """Wait for cancelled tasks, but never hang on a stuck cleanup."""

        pending = list(self.tasks)

        if self.main_task:
            pending.append(self.main_task)

        if not pending:
            return

        done, still = await asyncio.wait(pending, timeout=timeout)

        for task in done:
            if not task.cancelled():
                task.exception()  # mark as retrieved

        if still:
            print(
                "[TURN]", self.id,
                f"cleanup still running after {timeout}s; moving on "
                "(LLM/TTS stream is slow to close)."
            )

    # ---------------- main ----------------

    async def _run(self):

        call = self.call

        try:

            await call.send_json({"type": "turn_start", "turn_id": self.id})

            llm = self._spawn(
                self._fixed_loop() if self.fixed_text else self._llm_loop()
            )
            dispatch = self._spawn(self._dispatch_loop())
            sender = self._spawn(self._sender_loop())

            await asyncio.gather(llm, dispatch, sender)

            reply = "".join(self.generated).strip()

            self.finished = True

            metrics = {
                "llm_first_token_ms": _ms(self.t0, self.t_first_token),
                "first_chunk_ms": _ms(self.t0, self.t_first_chunk),
                "first_audio_ms": _ms(self.t0, self.t_first_audio),
                "total_ms": _ms(self.t0, time.perf_counter()),
            }

            print("[TURN]", self.id, "metrics:", metrics)

            await call.send_json({
                "type": "complete",
                "turn_id": self.id,
                "text": reply,
                "metrics": metrics,
            })

            if reply and not self.fixed_text:
                await asyncio.to_thread(
                    save_agent_response,
                    self.user_text, reply, call.session_id, call.user_id,
                )

        except asyncio.CancelledError:

            partial = "".join(self.generated).strip()

            print("[TURN]", self.id, "cancelled.")

            if self.audio_started and partial:
                call.interrupted_text = partial

            if self.audio_started and partial and not self.fixed_text:

                try:
                    await asyncio.to_thread(
                        save_agent_response,
                        self.user_text, partial, call.session_id,
                        call.user_id, True,
                    )
                except Exception:
                    pass

            self._cancel_children()

            raise

        except Exception as exc:

            print("[TURN ERROR]", repr(exc))
            traceback.print_exc()

            self._cancel_children()

            try:
                await call.send_json({
                    "type": "error",
                    "message": "Voice assistant failed. Check backend logs.",
                })
            except Exception:
                pass

        finally:
            self.finished = True

    def _cancel_children(self):
        for task in list(self.tasks):
            task.cancel()

    # ---------------- stage 1: LLM ----------------

    async def _fixed_loop(self):
        """Feed ready-made text into the normal TTS pipeline (no LLM)."""

        try:

            for chunk in self.fixed_text:

                self.generated.append(chunk + " ")

                now = time.perf_counter()

                if self.t_first_token is None:
                    self.t_first_token = now

                if self.t_first_chunk is None:
                    self.t_first_chunk = now

                await self.text_q.put(chunk)

        finally:
            self.text_q.put_nowait(None)

    async def _llm_loop(self):

        stripper = ThinkStripper()
        chunker = ClauseChunker()

        message = self.user_text

        note = self.call.interrupted_text
        self.call.interrupted_text = ""

        if note:
            message = (
                "[Note: your previous reply was cut off when the customer "
                f'started speaking. You had said: "{note}"]\n{message}'
            )

        stream = agent.arun(
            message,
            session_id=self.call.session_id,
            user_id=self.call.user_id,
            stream=True,
        )

        async def emit(visible):

            if not visible:
                return

            self.generated.append(visible)

            for chunk in chunker.add(visible):
                if self.t_first_chunk is None:
                    self.t_first_chunk = time.perf_counter()
                await self.text_q.put(chunk)

        try:

            events_logged = 0
            think_logged = False
            raw_parts = []

            async for event in stream:

                if events_logged < 8:
                    events_logged += 1
                    print(
                        "[LLM EVENT]",
                        getattr(event, "event", type(event).__name__),
                        f"(+{_ms(self.t0, time.perf_counter())} ms)",
                    )

                if getattr(event, "event", None) != RunEvent.run_content:
                    continue

                token = event.content

                if not token:
                    continue

                if self.t_first_token is None:
                    self.t_first_token = time.perf_counter()
                    print("[LLM] first token after",
                          _ms(self.t0, self.t_first_token), "ms")

                raw_parts.append(str(token))

                visible = stripper.feed(str(token))

                if stripper.in_think and not think_logged:
                    think_logged = True
                    print("[LLM] model is inside a <think> block "
                          "(reasoning tokens are hidden, so nothing is "
                          "spoken yet). Use a non-reasoning model or "
                          "disable thinking.")

                await emit(visible)

            await emit(stripper.finish())

            print(
                "[LLM] stream finished:",
                len("".join(self.generated)), "chars in",
                _ms(self.t0, time.perf_counter()), "ms",
            )

            raw = "".join(raw_parts)

            print(
                f"[LLM] raw output ({len(raw)} chars, spoken "
                f"{len(''.join(self.generated))}):",
                repr(raw[:500]) + ("..." if len(raw) > 500 else ""),
            )

            tail = chunker.flush()

            if tail:
                if self.t_first_chunk is None:
                    self.t_first_chunk = time.perf_counter()
                await self.text_q.put(tail)

        finally:

            self.text_q.put_nowait(None)

            aclose = getattr(stream, "aclose", None)

            if aclose:
                try:
                    await aclose()
                except Exception:
                    pass

    # ---------------- stage 2: start TTS per chunk ----------------

    async def _dispatch_loop(self):

        try:

            while True:

                chunk = await self.text_q.get()

                if chunk is None:
                    break

                print("[TTS CHUNK]", repr(chunk))

                await self.call.send_json({"type": "text_delta", "text": chunk})

                audio_q = asyncio.Queue()

                self._spawn(self._tts_fetch(chunk, audio_q))

                await self.order_q.put(audio_q)

        finally:
            self.order_q.put_nowait(None)

    async def _tts_fetch(self, text, audio_q):

        cached = TTS_CACHE.get(text)

        if cached:
            print("[TTS] served from cache:", repr(text))
            audio_q.put_nowait(cached)
            audio_q.put_nowait(None)
            return

        collect = [] if text in GREETING_SET else None
        finished_ok = False

        aligner = PCMAligner()

        t_req = time.perf_counter()
        total_bytes = 0

        try:

            async with self.tts_sem:

                async with GnaniTTSRealtimeClient(api_key=GNANI_API_KEY) as client:

                    stream = client.synthesize(
                        text,
                        voice=TTS_VOICE,
                        model=TTS_MODEL,
                        language="auto",
                        speed=1.0,
                        audio_config=TTS_AUDIO_CONFIG,
                    ).__aiter__()

                    got_audio = False

                    while True:

                        try:
                            raw = await asyncio.wait_for(
                                stream.__anext__(),
                                timeout=TTS_STALL_TIMEOUT,
                            )

                        except StopAsyncIteration:
                            finished_ok = True
                            break

                        except asyncio.TimeoutError:
                            print(
                                f"[TTS ERROR] Gnani sent no "
                                f"{'more ' if got_audio else ''}audio for "
                                f"{TTS_STALL_TIMEOUT:.0f}s, skipping chunk:",
                                repr(text),
                            )
                            break

                        if not raw:
                            continue

                        pcm = aligner.feed(raw)

                        if pcm:

                            if not got_audio:
                                print(
                                    "[TTS] first audio after",
                                    _ms(t_req, time.perf_counter()),
                                    "ms for", repr(text),
                                )

                            got_audio = True
                            total_bytes += len(pcm)
                            audio_q.put_nowait(pcm)

                            if collect is not None:
                                collect.append(pcm)

                    print(
                        "[TTS] chunk done:", total_bytes, "bytes in",
                        _ms(t_req, time.perf_counter()), "ms for", repr(text),
                    )

                    if collect and finished_ok and got_audio:
                        TTS_CACHE[text] = b"".join(collect)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            print("[TTS ERROR]", repr(exc))

        finally:
            audio_q.put_nowait(None)

    # ---------------- stage 3: ordered audio out ----------------

    async def _sender_loop(self):

        while True:

            audio_q = await self.order_q.get()

            if audio_q is None:
                break

            while True:

                pcm = await audio_q.get()

                if pcm is None:
                    break

                if not self.audio_started:
                    self.audio_started = True
                    self.t_first_audio = time.perf_counter()
                    print("[TURN]", self.id, "first audio after",
                          _ms(self.t0, self.t_first_audio), "ms")

                await self.call.send_audio(pcm)


class VoiceCall:
    """State for one browser call (one WebSocket)."""

    def __init__(self, ws, session_id, user_id):

        self.ws = ws
        self.session_id = session_id
        self.user_id = user_id

        self.send_lock = asyncio.Lock()

        self.turn = None
        self.turn_seq = 0

        # What the agent had said when it was interrupted (used once).
        self.interrupted_text = ""

        # User words from a turn that was cancelled before any audio played.
        self.pending_user_text = ""

        self.greeted = False

    # ---------------- sending ----------------

    async def send_json(self, payload):
        async with self.send_lock:
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_audio(self, data):
        async with self.send_lock:
            await self.ws.send_bytes(data)

    # ---------------- turns ----------------

    async def handle_user_utterance(self, text):

        text = (text or "").strip()

        if not text:
            return

        turn = self.turn

        if turn and turn.active:

            if turn.audio_started:
                # Assistant is already speaking. Real interruptions arrive
                # through the client's barge-in, so this is most likely
                # echo or a back-channel ("haan", "ok").
                print("[TURN] transcript during assistant speech ignored:", repr(text))
                return

            # Customer kept talking before any audio was produced:
            # drop the silent turn and merge both parts.
            turn.cancel()

        pending = self.pending_user_text
        self.pending_user_text = ""

        await self.start_turn(f"{pending} {text}".strip())

    async def start_turn(self, text):

        old = self.turn

        if old:
            old.cancel()
            await old.wait()

        self.turn_seq += 1

        print("\n[TURN]", self.turn_seq, "customer:", text)

        self.turn = Turn(self, self.turn_seq, text)
        self.turn.start()

    async def start_greeting(self):
        """Shivani speaks first. Triggered by the browser's "ready" message
        (sent once the microphone and barge-in are armed)."""

        if self.greeted or self.turn:
            return

        self.greeted = True

        self.turn_seq += 1

        print("\n[TURN]", self.turn_seq, "greeting")

        self.turn = Turn(
            self, self.turn_seq, "", fixed_text=list(GREETING_CHUNKS)
        )
        self.turn.start()

    async def handle_interrupt(self):

        turn = self.turn

        if turn and turn.active:
            print("[BARGE-IN] cancelling turn", turn.id)
            turn.cancel()

        await self.send_json({"type": "interrupted"})

    # ---------------- STT ----------------

    async def _stt_reader(self, stream):

        try:

            async for event in stream:

                if isinstance(event, StreamConnectedEvent):
                    await self.send_json({"type": "connected", "message": event.message})

                elif isinstance(event, StreamProcessingEvent):
                    await self.send_json({"type": "processing"})

                elif isinstance(event, StreamTranscriptEvent):

                    await self.send_json({
                        "type": "transcript",
                        "text": event.text,
                        "language": getattr(event, "detected_language_code", None),
                        "latency": getattr(event, "latency", None),
                    })

                    await self.handle_user_utterance(event.text)

                elif isinstance(event, StreamErrorEvent):
                    await self.send_json({"type": "error", "message": event.message})

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            print("[STT ERROR]", repr(exc))
            traceback.print_exc()

    # ---------------- main loop ----------------

    @staticmethod
    async def _close_stt(stt_client):
        """Close the Gnani STT socket, but never let a slow close hang the call."""

        close_task = asyncio.create_task(
            stt_client.__aexit__(None, None, None)
        )

        done, still = await asyncio.wait({close_task}, timeout=STT_CLOSE_TIMEOUT)

        if still:
            print(f"[STT] close took longer than {STT_CLOSE_TIMEOUT:.0f}s; "
                  "continuing without waiting.")
            return

        if not close_task.cancelled() and close_task.exception():
            print("[STT] close error:", repr(close_task.exception()))

    async def run(self):

        if not GNANI_API_KEY:
            await self.send_json({
                "type": "error",
                "message": "GNANI_API_KEY is missing from .env",
            })
            return

        stt_client = GnaniSTTStreamClient(
            api_key=GNANI_API_KEY,
            language_code="indic-auto",
            model="prisma-v3.0",
            language_candidates=["hi-IN", "en-IN"],
            sample_rate=16000,
        )

        stream = await stt_client.__aenter__()

        print("[STT] Connected to Gnani.")

        reader = asyncio.create_task(self._stt_reader(stream))

        try:

            while True:

                msg = await self.ws.receive()

                if msg["type"] == "websocket.disconnect":
                    break

                data = msg.get("bytes")

                if data is not None:

                    if len(data) == STT_FRAME_BYTES:
                        await stream.send_audio(data)

                    continue

                text = msg.get("text")

                if text is None:
                    continue

                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue

                kind = payload.get("type")

                if kind == "ready":
                    await self.start_greeting()

                elif kind == "interrupt":
                    await self.handle_interrupt()

                elif kind == "text" and ALLOW_TEXT_INPUT:
                    await self.handle_user_utterance(
                        str(payload.get("text") or "")
                    )

                elif kind == "stop":
                    break

        finally:

            reader.cancel()

            if self.turn:
                self.turn.cancel()
                await self.turn.wait()

            await asyncio.wait({reader}, timeout=1.0)

            await self._close_stt(stt_client)


# ==================================================
# 7. FASTAPI APP
# ==================================================

app = FastAPI(title="ABC Clothing Voice Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5501",
        "http://localhost:5501",
        "null",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def warm_greeting_cache():
    """Synthesize the greeting once so every call starts it instantly."""

    if not GNANI_API_KEY:
        return

    try:
        helper = Turn(None, 0, "")

        await asyncio.gather(*[
            helper._tts_fetch(chunk, asyncio.Queue())
            for chunk in GREETING_CHUNKS
        ])

        print(f"[GREETING] cache ready: {len(TTS_CACHE)}/{len(GREETING_CHUNKS)} chunks")

    except Exception as exc:
        print("[GREETING] warm-up failed (greeting will be synthesized live):", repr(exc))


@app.on_event("startup")
async def _startup():
    asyncio.create_task(warm_greeting_cache())


@app.get("/version")
async def version():
    """Open http://127.0.0.1:8000/version to see which code is really answering."""

    return {
        "version": APP_VERSION,
        "file": str(Path(__file__).resolve()),
        "pid": os.getpid(),
    }


@app.get("/health")
async def health():

    return {
        "success": True,
        "status": "ok",
        "service": "ABC Clothing Agent API",
    }


@app.post("/chat")
async def chat(request: Request):

    try:
        data = await request.json()
    except Exception:
        data = {}

    message = str(data.get("message") or "").strip()
    session_id = str(data.get("session_id") or "abc-demo-customer-001")
    user_id = str(data.get("user_id") or "demo-customer")

    if not message:
        return JSONResponse(
            {"success": False, "reply": "Please enter a message."},
            status_code=400,
        )

    try:

        reply = await asyncio.to_thread(
            get_agent_response, message, session_id, user_id
        )

        await asyncio.to_thread(
            save_agent_response, message, reply, session_id, user_id
        )

        return {"success": True, "reply": reply, "session_id": session_id}

    except Exception as exc:

        print("[CHAT ERROR]", repr(exc))
        traceback.print_exc()

        return JSONResponse(
            {"success": False, "reply": "Sorry, something went wrong. Please try again."},
            status_code=500,
        )


@app.websocket("/ws/voice")
async def voice_ws(ws: WebSocket):

    await ws.accept()

    session_id = ws.query_params.get("session_id") or "abc-demo-customer-001"
    user_id = ws.query_params.get("user_id") or "demo-customer"

    print("\n[VOICE WS] Connected:", session_id)

    call = VoiceCall(ws, session_id, user_id)

    try:
        await call.run()

    except WebSocketDisconnect:
        pass

    except Exception as exc:
        print("[VOICE WS ERROR]", repr(exc))
        traceback.print_exc()

    finally:
        print("[VOICE WS] Finished.")


@app.websocket("/voice/tts")
async def tts_test_ws(ws: WebSocket):
    """Standalone TTS test used by the 'Test TTS' button."""

    await ws.accept()

    try:

        request_data = json.loads(await ws.receive_text())
        text = str(request_data.get("text") or "").strip()

        if not text:
            await ws.send_text(json.dumps({"type": "error", "message": "No text received."}))
            return

        aligner = PCMAligner()
        count = 0

        async with GnaniTTSRealtimeClient(api_key=GNANI_API_KEY) as client:

            async for raw in client.synthesize(
                text,
                voice=TTS_VOICE,
                model=TTS_MODEL,
                language="auto",
                speed=1.0,
                audio_config=TTS_AUDIO_CONFIG,
            ):

                pcm = aligner.feed(raw) if raw else b""

                if pcm:
                    await ws.send_bytes(pcm)
                    count += 1

        await ws.send_text(json.dumps({"type": "complete", "chunks": count}))

    except WebSocketDisconnect:
        pass

    except Exception as exc:
        print("[TTS TEST ERROR]", repr(exc))


# ==================================================
# 8. RUN
# ==================================================

def _port_in_use(host, port):

    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":

    import sys
    import uvicorn

    # On Windows a second server can silently start on a port that an old
    # one already holds, and every request then goes to the OLD process.
    if _port_in_use("127.0.0.1", 8000):

        print("\n" + "!" * 60)
        print(" PORT 8000 IS ALREADY IN USE, so an OLD server is still running.")
        print(" Stop it first, then start this file again:")
        print("     netstat -ano | findstr :8000")
        print("     taskkill /PID <PID> /F")
        print(" (or:  Get-Process python | Stop-Process -Force)")
        print("!" * 60 + "\n")

        sys.exit(1)

    print("\n======================================")
    print(" ABC CLOTHING - REAL TIME VOICE AGENT")
    print("======================================")
    print(f"Version: {APP_VERSION}   PID: {os.getpid()}")
    print("Server: http://127.0.0.1:8000")
    print("Voice : ws://127.0.0.1:8000/ws/voice")
    print("LLM:", GROQ_MODEL)
    print("TTS:", TTS_VOICE, TTS_MODEL)
    print("======================================\n")

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
