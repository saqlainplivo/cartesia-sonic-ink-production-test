"""
Cartesia Ink-2 streaming STT client.

Uses: AsyncCartesia.stt.auto_finalize.websocket()
  - send_raw(bytes)  → send PCM audio chunks
  - send({"type":"close"})  → finalize and flush
  - recv()  → STTAutoFinalizeWebsocketResponse events
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional
import logging

log = logging.getLogger(__name__)


@dataclass
class TranscriptEvent:
    text: str
    is_final: bool
    ts: float


class CartesiaSTTClient:
    ENCODING = "pcm_s16le"
    SAMPLE_RATE = 8000

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        import cartesia
        self._api_key = api_key or os.environ["CARTESIA_API_KEY"]
        self._model = model or os.environ.get("CARTESIA_MODEL_INK_2", "ink-2")
        self._client = cartesia.AsyncCartesia(api_key=self._api_key)
        log.info("CartesiaSTTClient model=%s", self._model)

    async def transcribe_stream(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[TranscriptEvent]:
        try:
            async with self._client.stt.auto_finalize.websocket(
                encoding=self.ENCODING,
                model=self._model,
                sample_rate=self.SAMPLE_RATE,
            ) as conn:
                # Feed audio in a background task
                async def _feed():
                    async for chunk in audio_chunks:
                        if chunk:
                            await conn.send_raw(chunk)
                    # Signal end of audio — triggers final transcript
                    await conn.send({"type": "close"})

                feed_task = asyncio.create_task(_feed())

                # Receive transcript events
                # Event types from Cartesia Ink-2 auto_finalize websocket:
                #   connected      → session ready (skip)
                #   turn.start     → turn beginning (skip)
                #   turn.update    → partial rolling transcript
                #   turn.eager_end → turn silently ended, quasi-final
                #   turn.end       → final transcript, connection closes after
                try:
                    while True:
                        event = await asyncio.wait_for(conn.recv(), timeout=10.0)
                        ts = time.time()
                        event_type = str(getattr(event, "type", ""))
                        text = getattr(event, "transcript", None)

                        if event_type in ("connected", "turn.start") or not text:
                            continue

                        is_final = event_type == "turn.end"
                        yield TranscriptEvent(text=text.strip(), is_final=is_final, ts=ts)

                        if is_final:
                            break

                except asyncio.TimeoutError:
                    log.warning("STT recv timeout — no more events")
                except Exception as exc:
                    # Server closes connection after turn.end — normal
                    log.info("STT recv ended: %s", exc)
                finally:
                    await feed_task

        except Exception as exc:
            log.error("CartesiaSTT stream error: %s", exc)
            raise

    async def close(self):
        await self._client.close()


class MockSTTClient:
    def __init__(self, model: str = "mock-ink"):
        self._model = model

    async def transcribe_stream(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[TranscriptEvent]:
        log.info("[DRY-RUN] STT mock")
        async for _ in audio_chunks:
            pass
        await asyncio.sleep(0.3)
        yield TranscriptEvent(text="This is a dry-run mock transcript.", is_final=True, ts=time.time())

    async def close(self):
        pass


def get_stt_client(dry_run: bool = False, model: Optional[str] = None):
    if dry_run:
        return MockSTTClient(model=model or "mock-ink")
    return CartesiaSTTClient(model=model)
