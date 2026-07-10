"""
Live voice agent WebSocket handler.

Pipeline per turn:
  Plivo µ-law audio → PCM → Cartesia Ink-2 STT
  → Groq LLM (llama-3.3-70b-versatile)
  → Cartesia Sonic-3.5 TTS → µ-law → back into call

The agent answers the call, greets the caller, then listens and responds
in a loop until the caller hangs up or says goodbye.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import os
import time
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv()

import logging
log = logging.getLogger("agent")

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
CARTESIA_KEY   = os.environ.get("CARTESIA_API_KEY", "")
VOICE_ID       = os.environ.get("CARTESIA_VOICE_ID", "")
TTS_MODEL      = os.environ.get("CARTESIA_MODEL_SONIC_35", "sonic-2")
STT_MODEL      = os.environ.get("CARTESIA_MODEL_INK_2", "ink-2")

SYSTEM_PROMPT = """You are a friendly, concise voice assistant on a phone call.
Keep every response under 2 sentences — this is a voice call, not a chat window.
Be warm, natural, and conversational. If the caller says goodbye or thank you,
wrap up gracefully."""

GREETING = (
    "Hey there! This is a live Cartesia voice agent running over a real Plivo phone call. "
    "I'm using Sonic-3.5 for speech and Ink-2 to understand you. Go ahead — say anything!"
)


# ── LLM ──────────────────────────────────────────────────────────────────────

async def llm_reply(history: list[dict]) -> str:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    resp = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
        max_tokens=120,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


# ── TTS → µ-law chunks ────────────────────────────────────────────────────────

async def tts_to_mulaw(text: str) -> AsyncIterator[bytes]:
    import cartesia
    ac = cartesia.AsyncCartesia(api_key=CARTESIA_KEY)
    try:
        async with ac.tts.with_streaming_response.generate(
            model_id=TTS_MODEL,
            transcript=text,
            voice={"id": VOICE_ID},
            output_format={"container": "raw", "encoding": "pcm_s16le", "sample_rate": 8000},
        ) as resp:
            async for pcm_chunk in resp.iter_bytes(chunk_size=320):   # ~20ms
                yield audioop.lin2ulaw(pcm_chunk, 2)
    finally:
        await ac.close()


# ── STT ───────────────────────────────────────────────────────────────────────

async def stt_transcribe(pcm_chunks: list[bytes]) -> str:
    import cartesia
    ac = cartesia.AsyncCartesia(api_key=CARTESIA_KEY)
    audio = b"".join(pcm_chunks)
    final = ""
    try:
        async with ac.stt.auto_finalize.websocket(
            encoding="pcm_s16le", model=STT_MODEL, sample_rate=8000
        ) as conn:
            chunk_size = 4096
            for i in range(0, len(audio), chunk_size):
                await conn.send_raw(audio[i: i + chunk_size])
            await conn.send({"type": "close"})
            try:
                while True:
                    ev = await asyncio.wait_for(conn.recv(), timeout=8.0)
                    ev_type = str(getattr(ev, "type", ""))
                    text = getattr(ev, "transcript", None)
                    if ev_type == "turn.end" and text:
                        final = text.strip()
                        break
                    if ev_type == "turn.update" and text:
                        final = text.strip()   # keep updating partial
            except (asyncio.TimeoutError, Exception):
                pass
    finally:
        await ac.close()
    return final


# ── WebSocket call handler ────────────────────────────────────────────────────

async def run_agent(websocket, call_uuid: str):
    """
    Drive a full voice agent session over a Plivo bidirectional audio stream.

    Key design: inbound Plivo frames MUST be drained continuously —
    Plivo closes the WebSocket if the server stops reading its send buffer.
    We run a dedicated reader task in parallel with TTS sending.
    """
    log.info("Agent session started: %s", call_uuid)
    history: list[dict] = []

    # Shared state between reader and agent loop
    inbound_q: asyncio.Queue = asyncio.Queue()   # PCM chunks from caller
    stop_event = asyncio.Event()

    # ── Inbound reader (runs the whole call) ──────────────────────────────────
    async def inbound_reader():
        """Continuously drain Plivo WebSocket frames into inbound_q."""
        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                except asyncio.TimeoutError:
                    log.info("[%s] Inbound timeout", call_uuid)
                    stop_event.set()
                    break
                msg = json.loads(raw)
                ev = msg.get("event")
                if ev == "stop":
                    log.info("[%s] Stream stop", call_uuid)
                    stop_event.set()
                    break
                if ev == "media":
                    mulaw = base64.b64decode(msg["media"]["payload"])
                    pcm   = audioop.ulaw2lin(mulaw, 2)
                    await inbound_q.put(pcm)
        except Exception as exc:
            log.info("[%s] Reader ended: %s", call_uuid, exc)
            stop_event.set()

    # Flag: True while agent TTS is playing — discard inbound audio (prevent echo)
    agent_speaking = asyncio.Event()

    # ── TTS sender ────────────────────────────────────────────────────────────
    async def send_audio(text: str):
        log.info("[%s] TTS → %r", call_uuid, text[:80])
        agent_speaking.set()
        t0 = time.time()
        first = True
        async for mulaw_chunk in tts_to_mulaw(text):
            if stop_event.is_set():
                break
            if first:
                log.info("[%s] TTS first byte: %.0fms", call_uuid, (time.time()-t0)*1000)
                first = False
            payload = base64.b64encode(mulaw_chunk).decode()
            try:
                await websocket.send_json({
                    "event": "playAudio",
                    "media": {"contentType": "audio/x-mulaw;rate=8000", "payload": payload},
                })
            except Exception:
                stop_event.set()
                break
        # Wait a bit after TTS ends before listening (tail echo)
        await asyncio.sleep(0.8)
        # Drain any echo that accumulated during TTS
        while not inbound_q.empty():
            inbound_q.get_nowait()
        agent_speaking.clear()

    # ── Start inbound reader in background ───────────────────────────────────
    reader_task = asyncio.create_task(inbound_reader())

    # Wait for Plivo's "start" event before sending audio
    await asyncio.sleep(0.4)

    # ── Greeting ──────────────────────────────────────────────────────────────
    await send_audio(GREETING)

    # ── Listen → think → respond loop ─────────────────────────────────────────
    SILENCE_THRESHOLD = 60    # ~1.2s silence → end of turn
    MIN_SPEECH_CHUNKS = 15    # at least ~300ms of real speech
    VAD_RMS_FLOOR = 600       # raised — filters out codec noise and faint echo

    pcm_buffer: list[bytes] = []
    silence_chunks = 0

    try:
        while not stop_event.is_set():
            try:
                pcm = await asyncio.wait_for(inbound_q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                log.info("[%s] No audio for 30s, hanging up", call_uuid)
                await send_audio("I haven't heard anything for a while. Goodbye!")
                break

            # Discard while agent is speaking (echo suppression)
            if agent_speaking.is_set():
                continue

            rms = audioop.rms(pcm, 2)

            if rms < VAD_RMS_FLOOR:   # silence / background noise
                silence_chunks += 1
                if silence_chunks == SILENCE_THRESHOLD and len(pcm_buffer) >= MIN_SPEECH_CHUNKS:
                    captured = pcm_buffer[:]
                    pcm_buffer.clear()
                    silence_chunks = 0

                    log.info("[%s] Turn end — transcribing %d chunks", call_uuid, len(captured))
                    transcript = await stt_transcribe(captured)
                    log.info("[%s] Transcript: %r", call_uuid, transcript)

                    if not transcript:
                        continue

                    history.append({"role": "user", "content": transcript})
                    reply = await llm_reply(history)
                    history.append({"role": "assistant", "content": reply})
                    log.info("[%s] LLM reply: %r", call_uuid, reply)
                    await send_audio(reply)

                    if any(w in transcript.lower() for w in ("bye", "goodbye", "hang up", "that's all")):
                        await asyncio.sleep(0.5)
                        break
            else:
                silence_chunks = 0
                pcm_buffer.append(pcm)

    except Exception as exc:
        log.error("[%s] Agent error: %s", call_uuid, exc)
    finally:
        stop_event.set()
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
        log.info("[%s] Agent session ended", call_uuid)
