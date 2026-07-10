"""
Thin provider abstraction over Plivo Voice API.

The interface here is intentionally narrow so a Twilio backend could slot in
later without touching the orchestrator or eval code.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional
import logging

log = logging.getLogger(__name__)


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class CallResult:
    call_uuid: str
    status: str                    # "queued" | "ringing" | "in-progress" | "completed" | "failed"
    direction: str                 # "outbound" | "inbound"
    from_number: str
    to_number: str
    initiated_at: float            # unix timestamp
    error: Optional[str] = None


@dataclass
class RecordingResult:
    recording_id: str
    call_uuid: str
    url: str                       # public Plivo recording URL
    duration_seconds: float
    format: str = "mp3"


# ─── Provider interface ───────────────────────────────────────────────────────

class TelephonyProvider:
    """
    Minimal interface any provider backend must implement.
    """

    def place_call(
        self,
        to: str,
        answer_url: str,
        hangup_url: Optional[str] = None,
        record: bool = False,
    ) -> CallResult:
        raise NotImplementedError

    def hangup_call(self, call_uuid: str) -> bool:
        raise NotImplementedError

    def get_call_status(self, call_uuid: str) -> str:
        raise NotImplementedError

    def get_recording(self, recording_id: str) -> RecordingResult:
        raise NotImplementedError

    def build_stream_xml(
        self,
        stream_url: str,
        extra_verbs_xml: str = "",
        bidirectional: bool = True,
    ) -> str:
        """Return the TwiML/PHLO XML to open an audio stream on an active call."""
        raise NotImplementedError


# ─── Plivo implementation ─────────────────────────────────────────────────────

class PlivoProvider(TelephonyProvider):
    """
    Plivo Voice API backend.

    Requires:
        PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_FROM_NUMBER  in env.
    """

    def __init__(self) -> None:
        import plivo  # import here so the module loads without plivo installed in dry-run
        self._auth_id = os.environ["PLIVO_AUTH_ID"]
        self._auth_token = os.environ["PLIVO_AUTH_TOKEN"]
        self._from_number = os.environ["PLIVO_FROM_NUMBER"]
        self._client = plivo.RestClient(self._auth_id, self._auth_token)

    # ── outbound call ──────────────────────────────────────────────────────

    def place_call(
        self,
        to: str,
        answer_url: str,
        hangup_url: Optional[str] = None,
        record: bool = False,
    ) -> CallResult:
        initiated_at = time.time()
        try:
            params: dict = {
                "from_": self._from_number,
                "to_": to,
                "answer_url": answer_url,
                "answer_method": "POST",
            }
            if hangup_url:
                params["hangup_url"] = hangup_url
                params["hangup_method"] = "POST"
            if record:
                # Plivo records the full call when record=True in the call create
                params["record"] = True
                params["record_file_format"] = "mp3"

            response = self._client.calls.create(**params)
            call_uuid = response["request_uuid"]
            log.info("Placed call to %s, uuid=%s", to, call_uuid)
            return CallResult(
                call_uuid=call_uuid,
                status="queued",
                direction="outbound",
                from_number=self._from_number,
                to_number=to,
                initiated_at=initiated_at,
            )
        except Exception as exc:
            log.error("Failed to place call to %s: %s", to, exc)
            return CallResult(
                call_uuid="",
                status="failed",
                direction="outbound",
                from_number=self._from_number,
                to_number=to,
                initiated_at=initiated_at,
                error=str(exc),
            )

    def hangup_call(self, call_uuid: str) -> bool:
        try:
            self._client.calls.delete(call_uuid)
            return True
        except Exception as exc:
            log.error("Hangup failed for %s: %s", call_uuid, exc)
            return False

    def get_call_status(self, call_uuid: str) -> str:
        try:
            call = self._client.calls.get(call_uuid)
            return call["call_state"].lower()
        except Exception as exc:
            log.error("get_call_status(%s) failed: %s", call_uuid, exc)
            return "unknown"

    def get_recording(self, recording_id: str) -> RecordingResult:
        rec = self._client.recordings.get(recording_id)
        return RecordingResult(
            recording_id=recording_id,
            call_uuid=rec["call_uuid"],
            url=rec["record_url"],
            duration_seconds=float(rec.get("call_duration", 0)),
        )

    # ── XML helpers ────────────────────────────────────────────────────────

    def build_stream_xml(
        self,
        stream_url: str,
        extra_verbs_xml: str = "",
        bidirectional: bool = True,
    ) -> str:
        """
        Return Plivo XML that opens a bidirectional audio WebSocket stream.

        Plivo's <Stream> verb sends μ-law (G.711) 8 kHz audio over a WebSocket.
        `bidirectional="true"` lets the server push TTS audio back into the call.
        """
        direction_attr = 'bidirectional="true"' if bidirectional else ""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream {direction_attr} keepCallAlive="true" streamTimeout="300" contentType="audio/x-mulaw;rate=8000">{stream_url}</Stream>
    {extra_verbs_xml}
</Response>"""

    def build_record_xml(self, callback_url: str) -> str:
        """XML that records the call and POSTs metadata to callback_url on completion."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Record action="{callback_url}" maxLength="120" recordSession="false" />
</Response>"""


# ─── Mock provider (dry-run) ──────────────────────────────────────────────────

class MockProvider(TelephonyProvider):
    """
    Fake provider that logs calls but never touches Plivo.
    Used with --dry-run to exercise the full pipeline without spending money.
    """

    def __init__(self) -> None:
        self._call_counter = 0

    def place_call(self, to, answer_url, hangup_url=None, record=False) -> CallResult:
        self._call_counter += 1
        uuid = f"mock-{self._call_counter:04d}"
        log.info("[DRY-RUN] place_call to=%s uuid=%s answer_url=%s", to, uuid, answer_url)
        return CallResult(
            call_uuid=uuid,
            status="queued",
            direction="outbound",
            from_number="+10000000000",
            to_number=to,
            initiated_at=time.time(),
        )

    def hangup_call(self, call_uuid: str) -> bool:
        log.info("[DRY-RUN] hangup_call uuid=%s", call_uuid)
        return True

    def get_call_status(self, call_uuid: str) -> str:
        return "completed"

    def get_recording(self, recording_id: str) -> RecordingResult:
        return RecordingResult(
            recording_id=recording_id,
            call_uuid="mock-0000",
            url="https://example.com/mock-recording.mp3",
            duration_seconds=10.0,
        )

    def build_stream_xml(self, stream_url, extra_verbs_xml="", bidirectional=True) -> str:
        log.info("[DRY-RUN] build_stream_xml url=%s", stream_url)
        return f"<!-- DRY-RUN stream XML for {stream_url} -->"


def get_provider(dry_run: bool = False) -> TelephonyProvider:
    """Factory — returns MockProvider in dry-run mode, PlivoProvider otherwise."""
    if dry_run:
        return MockProvider()
    return PlivoProvider()
