"""
Cartesia Ink-2 streaming STT client.

Consumes raw µ-law or PCM audio chunks from a Plivo audio stream and returns
real-time transcript events (partial + final).
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
    ts: float  # time.time() when the event was received from Cartesia


class CartesiaSTTClient:
    """
    Wraps Cartesia's streaming STT (Ink-2) for real-time transcription
    of telephony audio.

    Expected audio: PCM 16-bit signed little-endian at 8 kHz (after µ-law decode).
    """

    INPUT_ENCODING = "pcm_s16le"
    SAMPLE_RATE = 8000

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        import cartesia
        self._api_key = api_key or os.environ["CARTESIA_API_KEY"]
        self._model = model or os.environ.get("CARTESIA_MODEL_INK_2", "ink-2")
        self._client = cartesia.AsyncCartesia(api_key=self._api_key)
        log.info("CartesiaSTTClient model=%s", self._model)

    async def transcribe_stream(
        self,
        audio_chunks: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        """
        Feed an async iterator of raw PCM audio and yield TranscriptEvents.

        Cartesia's streaming STT API accepts audio as chunked bytes over a
        WebSocket connection.  We send chunks as they arrive from the call leg
        and yield transcript events as they come back.
        """
        try:
            async with self._client.stt.stream(
                model=self._model,
                encoding=self.INPUT_ENCODING,
                sample_rate=self.SAMPLE_RATE,
                language="en",
            ) as session:
                async def _feed():
                    async for chunk in audio_chunks:
                        await session.send(chunk)
                    await session.send_eof()

                feed_task = asyncio.create_task(_feed())

                async for event in session:
                    ts = time.time()
                    if hasattr(event, "transcript"):
                        is_final = getattr(event, "is_final", False)
                        text = event.transcript
                        if text:
                            yield TranscriptEvent(text=text, is_final=is_final, ts=ts)

                await feed_task

        except Exception as exc:
            log.error("CartesiaSTT stream error: %s", exc)
            raise

    async def close(self) -> None:
        await self._client.close()


class MockSTTClient:
    """Dry-run stand-in: echoes a fixed transcript after a simulated delay."""

    def __init__(self, model: str = "mock-ink") -> None:
        self._model = model

    async def transcribe_stream(
        self,
        audio_chunks: AsyncIterator[bytes],
    ) -> AsyncIterator[TranscriptEvent]:
        log.info("[DRY-RUN] STT mock transcription running")
        # Drain the audio stream
        async for _ in audio_chunks:
            pass
        await asyncio.sleep(0.3)
        yield TranscriptEvent(
            text="This is a dry-run mock transcript.",
            is_final=False,
            ts=time.time(),
        )
        await asyncio.sleep(0.1)
        yield TranscriptEvent(
            text="This is a dry-run mock transcript.",
            is_final=True,
            ts=time.time(),
        )

    async def close(self) -> None:
        pass


def get_stt_client(dry_run: bool = False, model: Optional[str] = None):
    if dry_run:
        return MockSTTClient(model=model or "mock-ink")
    return CartesiaSTTClient(model=model)
