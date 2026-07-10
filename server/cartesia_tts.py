"""
Cartesia Sonic TTS streaming client.

Model is swappable via CARTESIA_TTS_MODEL env var or constructor argument,
so Sonic-3.5 and Sonic-3 run through the identical pipeline for a fair comparison.

Audio format: PCM 16-bit 8 kHz (µ-law compatible with Plivo's stream).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncIterator, Optional
import logging

log = logging.getLogger(__name__)


class CartesiaTTSClient:
    """
    Async streaming TTS client wrapping the Cartesia Python SDK.

    Yields raw PCM audio chunks as bytes.
    Records timing hooks so the latency logger can be driven externally.
    """

    # Plivo <Stream> sends/receives µ-law 8 kHz; Cartesia can output PCM 8000
    OUTPUT_FORMAT = {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 8000,
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        voice_id: Optional[str] = None,
    ) -> None:
        import cartesia  # late import to allow dry-run without the SDK installed
        self._api_key = api_key or os.environ["CARTESIA_API_KEY"]
        self._model = model or os.environ.get("CARTESIA_TTS_MODEL", os.environ.get("CARTESIA_MODEL_SONIC_35", "sonic-2"))
        self._voice_id = voice_id or os.environ["CARTESIA_VOICE_ID"]
        self._client = cartesia.AsyncCartesia(api_key=self._api_key)
        log.info("CartesiaTTSClient model=%s voice=%s", self._model, self._voice_id)

    async def stream(
        self,
        text: str,
        on_first_byte: Optional[callable] = None,
    ) -> AsyncIterator[bytes]:
        """
        Yield PCM audio chunks for `text`.

        `on_first_byte` is called (with no args) on the first audio chunk,
        so the caller can record TTS_FIRST_AUDIO_BYTE in the latency log.
        """
        first = True
        try:
            async with self._client.tts.bytes(
                model_id=self._model,
                transcript=text,
                voice={"id": self._voice_id},
                output_format=self.OUTPUT_FORMAT,
                stream=True,
            ) as response:
                async for chunk in response:
                    if chunk:
                        if first:
                            first = False
                            if on_first_byte:
                                on_first_byte()
                        yield chunk
        except Exception as exc:
            log.error("CartesiaTTS stream error: %s", exc)
            raise

    async def close(self) -> None:
        await self._client.close()


class MockTTSClient:
    """Dry-run stand-in: generates silent PCM frames without calling Cartesia."""

    OUTPUT_FORMAT = CartesiaTTSClient.OUTPUT_FORMAT

    def __init__(self, model: str = "mock-sonic") -> None:
        self._model = model

    async def stream(
        self,
        text: str,
        on_first_byte: Optional[callable] = None,
    ) -> AsyncIterator[bytes]:
        log.info("[DRY-RUN] TTS mock stream: model=%s text=%r", self._model, text[:60])
        # 0.5 s of silence at 8 kHz PCM-16 = 8000 samples × 2 bytes = 8000 bytes
        silence = b"\x00\x00" * 4000
        chunk_size = 320  # ~20 ms chunks
        first = True
        for i in range(0, len(silence), chunk_size):
            await asyncio.sleep(0.02)
            chunk = silence[i : i + chunk_size]
            if first:
                first = False
                if on_first_byte:
                    on_first_byte()
            yield chunk

    async def close(self) -> None:
        pass


def get_tts_client(dry_run: bool = False, model: Optional[str] = None):
    if dry_run:
        return MockTTSClient(model=model or "mock-sonic")
    return CartesiaTTSClient(model=model)
