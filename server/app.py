"""
FastAPI orchestrator server.

Handles three concerns:
  1. Plivo webhook endpoints (answer URL, hangup URL, recording callback).
  2. WebSocket bridge: receives µ-law audio from Plivo's <Stream>, routes it
     to Cartesia Ink-2 STT, and pushes Sonic TTS audio back into the call.
  3. Latency instrumentation at every pipeline boundary.

Run:
    uvicorn server.app:app --port 5000 --reload

In production expose this through ngrok or a cloud host so Plivo can reach it.
"""

from __future__ import annotations

import asyncio
import audioop  # stdlib µ-law ↔ PCM conversion (Python 3.12-)
import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
import logging

load_dotenv()

from server.cartesia_tts import get_tts_client
from server.cartesia_stt import get_stt_client, TranscriptEvent
from server.latency_logger import CallLog, Stage, LOGS_DIR
from server.agent import run_agent
from telephony.provider import get_provider

# ─── Config ───────────────────────────────────────────────────────────────────

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "http://localhost:5000").rstrip("/")
SONIC_35_MODEL = os.environ.get("CARTESIA_MODEL_SONIC_35", "sonic-2")
SONIC_3_MODEL = os.environ.get("CARTESIA_MODEL_SONIC_3", "sonic")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
log = logging.getLogger("server.app")

app = FastAPI(title="Cartesia × Plivo Eval Server")

# ─── In-memory call state ─────────────────────────────────────────────────────
# Maps call_uuid → CallLog.  A real system would use Redis; fine for eval.
_call_logs: dict[str, CallLog] = {}


def _get_or_create_log(call_uuid: str, **kwargs) -> CallLog:
    if call_uuid not in _call_logs:
        _call_logs[call_uuid] = CallLog(call_uuid=call_uuid, **kwargs)
    return _call_logs[call_uuid]


# ─── Webhook: answer ──────────────────────────────────────────────────────────

@app.post("/webhooks/answer")
async def answer(request: Request):
    """
    Plivo calls this URL when the callee answers.
    We reply with XML that opens a bidirectional audio stream back to /ws/stream.
    """
    form = await request.form()
    call_uuid = form.get("CallUUID", str(uuid.uuid4()))
    script_id = form.get("script_id", "unknown")
    category = form.get("category", "unknown")
    model = form.get("model", SONIC_35_MODEL)
    mode = form.get("mode", "roundtrip")

    clog = _get_or_create_log(
        call_uuid,
        script_id=script_id,
        category=category,
        model=model,
        mode=mode,
        dry_run=DRY_RUN,
    )
    clog.mark(Stage.CALL_ANSWERED)

    stream_url = f"{WEBHOOK_BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/ws/stream/{call_uuid}"
    provider = get_provider(dry_run=DRY_RUN)
    xml = provider.build_stream_xml(stream_url=stream_url)
    log.info("Answer webhook call_uuid=%s → streaming to %s", call_uuid, stream_url)
    return Response(content=xml, media_type="application/xml")


# ─── Webhook: hangup ──────────────────────────────────────────────────────────

@app.post("/webhooks/hangup")
async def hangup(request: Request):
    form = await request.form()
    call_uuid = form.get("CallUUID", "")
    if call_uuid in _call_logs:
        clog = _call_logs[call_uuid]
        clog.mark(Stage.CALL_HANGUP)
        clog.finalize()
        clog.save()
        log.info("Call %s ended; log saved.", call_uuid)
    return PlainTextResponse("OK")


# ─── Webhook: recording ───────────────────────────────────────────────────────

@app.post("/webhooks/recording")
async def recording_callback(request: Request):
    form = await request.form()
    call_uuid = form.get("CallUUID", "")
    rec_url = form.get("RecordUrl", "")
    if call_uuid in _call_logs:
        _call_logs[call_uuid].mark("recording_ready", note=rec_url)
    log.info("Recording ready: call=%s url=%s", call_uuid, rec_url)
    return PlainTextResponse("OK")


# ─── WebSocket: audio bridge ──────────────────────────────────────────────────

@app.websocket("/ws/stream/{call_uuid}")
async def audio_stream(websocket: WebSocket, call_uuid: str):
    """
    Plivo <Stream> WebSocket endpoint.

    Protocol (Plivo bidirectional stream):
      • Inbound messages: JSON  {"event": "media", "media": {"payload": <base64 µ-law>}}
      • Outbound messages: JSON {"event": "playAudio", "media": {"payload": <base64 µ-law>}}
      • Control:           JSON {"event": "start"/"stop"/...}

    Pipeline:
      µ-law bytes → PCM-16 decode → Cartesia Ink-2 (STT)
      STT final transcript → Cartesia Sonic (TTS) → PCM chunks → µ-law encode → Plivo
    """
    await websocket.accept()
    clog = _call_logs.get(call_uuid)
    if clog is None:
        clog = _get_or_create_log(
            call_uuid,
            script_id="inbound",
            category="inbound",
            model=SONIC_35_MODEL,
            mode="roundtrip",
        )

    clog.mark(Stage.STREAM_OPENED)
    log.info("WebSocket stream opened for call %s", call_uuid)

    # Audio queue: raw PCM-16 chunks fed by inbound audio handler
    audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()

    stt_client = get_stt_client(dry_run=DRY_RUN)
    tts_client = get_tts_client(dry_run=DRY_RUN, model=clog.model)

    # Accumulate the STT final transcript so we can compute WER later
    transcript_parts: list[str] = []

    async def inbound_reader():
        """Read µ-law frames from Plivo, decode to PCM, push to audio_queue."""
        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "start":
                    log.info("Stream started: %s", msg.get("start", {}))

                elif event == "media":
                    payload_b64 = msg["media"]["payload"]
                    mulaw_bytes = base64.b64decode(payload_b64)
                    # Convert µ-law → signed 16-bit PCM (stdlib audioop)
                    pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                    await audio_queue.put(pcm_bytes)

                elif event == "stop":
                    log.info("Stream stop event for call %s", call_uuid)
                    clog.mark(Stage.SPEECH_END_DETECTED)
                    await audio_queue.put(None)  # sentinel
                    break

        except WebSocketDisconnect:
            log.info("WebSocket disconnected for call %s", call_uuid)
            await audio_queue.put(None)
        except Exception as exc:
            log.error("inbound_reader error call=%s: %s", call_uuid, exc)
            await audio_queue.put(None)

    async def pcm_audio_iter():
        """Async iterator over PCM chunks from the queue."""
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                return
            yield chunk

    async def stt_and_tts():
        """
        Run STT on inbound audio, then run TTS on the final transcript
        and push audio back into the call.
        """
        final_text = ""
        async for event in stt_client.transcribe_stream(pcm_audio_iter()):
            if not event.is_final:
                clog.mark(Stage.STT_PARTIAL_RECEIVED, note=event.text[:80])
            else:
                clog.mark(Stage.STT_FINAL_RECEIVED, note=event.text[:80])
                transcript_parts.append(event.text)
                final_text = event.text
                log.info("STT final [%s]: %r", call_uuid, final_text)

        if not final_text:
            log.warning("No STT transcript for call %s — skipping TTS.", call_uuid)
            return

        # Simple canned response for the round-trip test
        response_text = f"Got it. You said: {final_text}"
        clog.mark(Stage.TTS_REQUEST_SENT, note=response_text[:80])

        first_byte_sent = False

        async def _note_first_byte():
            nonlocal first_byte_sent
            if not first_byte_sent:
                first_byte_sent = True
                clog.mark(Stage.TTS_FIRST_AUDIO_BYTE)
                clog.mark(Stage.FIRST_AGENT_AUDIO_BYTE)

        async for pcm_chunk in tts_client.stream(response_text, on_first_byte=_note_first_byte):
            # Re-encode PCM → µ-law for Plivo
            mulaw_chunk = audioop.lin2ulaw(pcm_chunk, 2)
            payload = base64.b64encode(mulaw_chunk).decode()
            await websocket.send_json({
                "event": "playAudio",
                "media": {"contentType": "audio/x-mulaw;rate=8000", "payload": payload},
            })

        clog.mark(Stage.TTS_STREAM_COMPLETE)

    # Run inbound reader and STT+TTS pipeline concurrently
    await asyncio.gather(inbound_reader(), stt_and_tts())

    clog.transcript = " ".join(transcript_parts)
    await stt_client.close()
    await tts_client.close()
    log.info("Stream pipeline complete for call %s", call_uuid)


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "dry_run": DRY_RUN}


# ─── Live agent endpoints ──────────────────────────────────────────────────────

@app.post("/agent/answer")
async def agent_answer(request: Request):
    """Answer webhook for live agent demo calls."""
    form = await request.form()
    call_uuid = form.get("CallUUID", str(uuid.uuid4()))
    log.info("Agent answer: call_uuid=%s", call_uuid)
    stream_url = (
        WEBHOOK_BASE_URL
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + f"/agent/stream/{call_uuid}"
    )
    provider = get_provider(dry_run=False)
    xml = provider.build_stream_xml(stream_url=stream_url)
    return Response(content=xml, media_type="application/xml")


@app.websocket("/agent/stream/{call_uuid}")
async def agent_stream(websocket: WebSocket, call_uuid: str):
    """Live agent WebSocket — Ink-2 STT → Groq LLM → Sonic TTS."""
    await websocket.accept()
    log.info("Agent stream connected: %s", call_uuid)
    await run_agent(websocket, call_uuid)
