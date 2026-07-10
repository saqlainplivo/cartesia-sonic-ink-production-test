"""
Cartesia Sonic TTS streaming client.

Uses: AsyncCartesia.tts.with_streaming_response.generate()
      → iter_bytes(chunk_size) for streaming PCM audio.

Model is swappable via constructor arg or CARTESIA_TTS_MODEL env var,
so Sonic-3.5 (sonic-2) and Sonic-3 (sonic) run through the identical pipeline.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncIterator, Optional
import logging

log = logging.getLogger(__name__)

OUTPUT_FORMAT = {
    "container": "raw",
    "encoding": "pcm_s16le",
    "sample_rate": 8000,
}


class CartesiaTTSClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, voice_id: Optional[str] = None):
        import cartesia
        self._api_key = api_key or os.environ["CARTESIA_API_KEY"]
        self._model = model or os.environ.get("CARTESIA_TTS_MODEL") or os.environ.get("CARTESIA_MODEL_SONIC_35", "sonic-2")
        self._voice_id = voice_id or os.environ["CARTESIA_VOICE_ID"]
        self._client = cartesia.AsyncCartesia(api_key=self._api_key)
        log.info("CartesiaTTSClient model=%s voice=%s", self._model, self._voice_id)

    async def stream(self, text: str, on_first_byte: Optional[callable] = None) -> AsyncIterator[bytes]:
        first = True
        try:
            async with self._client.tts.with_streaming_response.generate(
                model_id=self._model,
                transcript=text,
                voice={"id": self._voice_id},
                output_format=OUTPUT_FORMAT,
            ) as resp:
                async for chunk in resp.iter_bytes(chunk_size=4096):
                    if chunk:
                        if first:
                            first = False
                            if on_first_byte:
                                on_first_byte()
                        yield chunk
        except Exception as exc:
            log.error("CartesiaTTS stream error: %s", exc)
            raise

    async def close(self):
        await self._client.close()


class MockTTSClient:
    OUTPUT_FORMAT = OUTPUT_FORMAT

    def __init__(self, model: str = "mock-sonic"):
        self._model = model

    async def stream(self, text: str, on_first_byte: Optional[callable] = None) -> AsyncIterator[bytes]:
        log.info("[DRY-RUN] TTS mock stream model=%s text=%r", self._model, text[:60])
        silence = b"\x00\x00" * 4000
        chunk_size = 320
        first = True
        for i in range(0, len(silence), chunk_size):
            await asyncio.sleep(0.02)
            chunk = silence[i: i + chunk_size]
            if first:
                first = False
                if on_first_byte:
                    on_first_byte()
            yield chunk

    async def close(self):
        pass


def get_tts_client(dry_run: bool = False, model: Optional[str] = None):
    if dry_run:
        return MockTTSClient(model=model or "mock-sonic")
    return CartesiaTTSClient(model=model)
