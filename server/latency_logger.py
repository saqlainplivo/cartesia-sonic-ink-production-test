"""
Structured per-call latency logger.

Records timestamps at every pipeline stage and writes a JSON log file per call.
Stage names are deliberately explicit so the aggregation script can diff them
without magic.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
import logging

log = logging.getLogger(__name__)

LOGS_DIR = Path(os.getenv("LOGS_DIR", "logs"))


@dataclass
class LatencyEvent:
    stage: str
    ts: float                    # Unix timestamp (seconds, float)
    note: Optional[str] = None   # free-form annotation


@dataclass
class CallLog:
    call_uuid: str
    script_id: str
    category: str
    model: str                   # sonic-3.5 | sonic-3
    mode: str                    # tts_focused | stt_focused | roundtrip
    dry_run: bool = False

    events: list[LatencyEvent] = field(default_factory=list)

    # Derived fields (populated by finalize())
    call_setup_ms: Optional[float] = None        # call_answered - call_initiated
    tts_first_byte_ms: Optional[float] = None    # tts_first_audio - tts_request_sent
    tts_stream_complete_ms: Optional[float] = None
    stt_final_ms: Optional[float] = None         # stt_final_received - speech_end_detected
    end_to_end_ms: Optional[float] = None        # first_agent_audio - speech_end_detected
    error: Optional[str] = None
    transcript: Optional[str] = None
    wer: Optional[float] = None

    # ── stage tracking ─────────────────────────────────────────────────────

    def mark(self, stage: str, note: Optional[str] = None) -> float:
        """Record a named stage and return the timestamp."""
        ts = time.time()
        self.events.append(LatencyEvent(stage=stage, ts=ts, note=note))
        log.debug("[%s] stage=%s", self.call_uuid, stage)
        return ts

    def _ts(self, stage: str) -> Optional[float]:
        for e in self.events:
            if e.stage == stage:
                return e.ts
        return None

    def _delta_ms(self, from_stage: str, to_stage: str) -> Optional[float]:
        t0 = self._ts(from_stage)
        t1 = self._ts(to_stage)
        if t0 is not None and t1 is not None:
            return (t1 - t0) * 1000.0
        return None

    # ── finalize ───────────────────────────────────────────────────────────

    def finalize(self) -> None:
        """Compute derived latency fields from recorded events."""
        self.call_setup_ms = self._delta_ms("call_initiated", "call_answered")
        self.tts_first_byte_ms = self._delta_ms("tts_request_sent", "tts_first_audio_byte")
        self.tts_stream_complete_ms = self._delta_ms("tts_request_sent", "tts_stream_complete")
        self.stt_final_ms = self._delta_ms("speech_end_detected", "stt_final_received")
        self.end_to_end_ms = self._delta_ms("speech_end_detected", "first_agent_audio_byte")

    # ── persistence ────────────────────────────────────────────────────────

    def save(self) -> Path:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / f"{self.call_uuid}.json"
        data = asdict(self)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        log.info("Saved call log → %s", path)
        return path

    @classmethod
    def load(cls, path: Path) -> "CallLog":
        with open(path) as f:
            data = json.load(f)
        events = [LatencyEvent(**e) for e in data.pop("events", [])]
        obj = cls(**{k: v for k, v in data.items() if k != "events"})
        obj.events = events
        return obj


# ─── Stage name constants ──────────────────────────────────────────────────────
# Use these everywhere to prevent typos.

class Stage:
    CALL_INITIATED = "call_initiated"
    CALL_ANSWERED = "call_answered"
    STREAM_OPENED = "stream_opened"
    SPEECH_START_DETECTED = "speech_start_detected"
    SPEECH_END_DETECTED = "speech_end_detected"
    STT_PARTIAL_RECEIVED = "stt_partial_received"
    STT_FINAL_RECEIVED = "stt_final_received"
    TTS_REQUEST_SENT = "tts_request_sent"
    TTS_FIRST_AUDIO_BYTE = "tts_first_audio_byte"
    TTS_STREAM_COMPLETE = "tts_stream_complete"
    FIRST_AGENT_AUDIO_BYTE = "first_agent_audio_byte"
    CALL_HANGUP = "call_hangup"
