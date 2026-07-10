#!/usr/bin/env python3
"""
run_eval.py — top-level eval runner.

Orchestrates test calls across the three modes:
  tts     — place an outbound call, stream TTS audio, record, check non-verbal fidelity
  stt     — play a reference script into a call, capture Ink-2 transcript, score WER
  roundtrip — single-turn voice agent; measure true end-to-end latency

Usage:
    # Dry run (no real calls, no charges)
    python run_eval.py --dry-run --mode all

    # Real calls — TTS mode only, 3 repeats, Sonic-3.5
    python run_eval.py --mode tts --repeats 3 --model sonic-3.5

    # Real calls — all modes, both models, 5 repeats each
    python run_eval.py --mode all --repeats 5 --both-models

    # Aggregate logs already collected
    python run_eval.py --aggregate-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

from telephony.provider import get_provider
from server.cartesia_tts import get_tts_client
from server.cartesia_stt import get_stt_client
from server.latency_logger import CallLog, Stage, LOGS_DIR
from eval.wer import score as wer_score, CARTESIA_PUBLISHED_WER_BASELINE

console = Console()

MAX_CALLS = int(os.getenv("MAX_REAL_CALLS_PER_RUN", "10"))
REPEATS_DEFAULT = int(os.getenv("REPEATS_PER_CATEGORY", "5"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "http://localhost:5000")
SONIC_35 = os.getenv("CARTESIA_MODEL_SONIC_35", "sonic-2")
SONIC_3 = os.getenv("CARTESIA_MODEL_SONIC_3", "sonic")
INK_2 = os.getenv("CARTESIA_MODEL_INK_2", "ink-2")


def load_scripts() -> list[dict]:
    p = Path("test_scripts/scripts.json")
    with open(p) as f:
        data = json.load(f)
    return data["categories"]


# ─── TTS-focused test ─────────────────────────────────────────────────────────

async def run_tts_test(
    script: dict,
    category: str,
    model: str,
    dry_run: bool,
) -> CallLog:
    """
    Stream TTS audio for `script` through a real/mock call.
    Records whether non-verbal tags were preserved as audio.
    """
    call_uuid = str(uuid.uuid4())[:8]
    clog = CallLog(
        call_uuid=call_uuid,
        script_id=script["id"],
        category=category,
        model=model,
        mode="tts_focused",
        dry_run=dry_run,
    )
    clog.mark(Stage.CALL_INITIATED)

    provider = get_provider(dry_run=dry_run)
    to_number = os.getenv("PLIVO_TO_NUMBER", "+10000000000")
    answer_url = (
        f"{WEBHOOK_BASE_URL}/webhooks/answer"
        f"?script_id={script['id']}&category={category}&model={model}&mode=tts_focused"
    )

    result = provider.place_call(
        to=to_number,
        answer_url=answer_url,
        hangup_url=f"{WEBHOOK_BASE_URL}/webhooks/hangup",
        record=True,
    )
    clog.call_uuid = result.call_uuid or call_uuid

    if result.error:
        clog.error = result.error
        clog.finalize()
        clog.save()
        return clog

    if dry_run:
        # In dry-run, simulate TTS locally and measure timing
        clog.mark(Stage.CALL_ANSWERED)
        tts = get_tts_client(dry_run=True, model=model)
        clog.mark(Stage.TTS_REQUEST_SENT)
        first = True
        async for chunk in tts.stream(script["text"], on_first_byte=lambda: clog.mark(Stage.TTS_FIRST_AUDIO_BYTE)):
            pass
        clog.mark(Stage.TTS_STREAM_COMPLETE)
        clog.mark(Stage.CALL_HANGUP)

        # Non-verbal fidelity: in dry-run we can't measure audio, note that explicitly
        nonverbal_tags = script.get("nonverbal_tags", [])
        if nonverbal_tags:
            console.print(f"  [yellow][DRY-RUN] Non-verbal fidelity requires real call recording — cannot measure in dry-run.[/yellow]")

    clog.finalize()
    clog.save()
    return clog


# ─── STT-focused test ─────────────────────────────────────────────────────────

async def run_stt_test(
    script: dict,
    category: str,
    model_tts: str,
    dry_run: bool,
) -> CallLog:
    """
    Play a known-text TTS clip into a call, transcribe with Ink-2, compute WER.
    """
    call_uuid = str(uuid.uuid4())[:8]
    clog = CallLog(
        call_uuid=call_uuid,
        script_id=script["id"],
        category=category,
        model=model_tts,
        mode="stt_focused",
        dry_run=dry_run,
    )
    clog.mark(Stage.CALL_INITIATED)

    tts = get_tts_client(dry_run=dry_run, model=model_tts)
    stt = get_stt_client(dry_run=dry_run)

    # Generate TTS audio into a buffer
    clog.mark(Stage.TTS_REQUEST_SENT)
    audio_buffer: list[bytes] = []
    async for chunk in tts.stream(script["text"], on_first_byte=lambda: clog.mark(Stage.TTS_FIRST_AUDIO_BYTE)):
        audio_buffer.append(chunk)
    clog.mark(Stage.TTS_STREAM_COMPLETE)

    # Transcribe with STT
    async def _audio_iter():
        for chunk in audio_buffer:
            yield chunk

    final_text = ""
    async for event in stt.transcribe_stream(_audio_iter()):
        if event.is_final:
            clog.mark(Stage.STT_FINAL_RECEIVED, note=event.text[:80])
            final_text = event.text

    clog.transcript = final_text

    # WER scoring
    reference = script.get("reference_text", script["text"])
    nonverbal_tags = script.get("nonverbal_tags")
    if final_text:
        result = wer_score(final_text, reference, nonverbal_tags)
        clog.wer = result.wer
        # Store non-verbal leak result in the log (saved as JSON)
        if nonverbal_tags:
            # Inject into the saved JSON via a monkey-patch on the log dict
            clog.__dict__["nonverbal_leaked"] = result.nonverbal_leaked_in_hypothesis
            if result.nonverbal_leaked_in_hypothesis:
                console.print(f"  [red]NON-VERBAL LEAK: {result.leak_details}[/red]")
        console.print(
            f"  WER={result.wer:.3f} (baseline {CARTESIA_PUBLISHED_WER_BASELINE:.3f}, "
            f"Δ={result.wer - CARTESIA_PUBLISHED_WER_BASELINE:+.3f})"
        )
    else:
        clog.error = "No STT transcript received"
        console.print("  [red]ERROR: No transcript received[/red]")

    clog.mark(Stage.CALL_HANGUP)
    clog.finalize()
    clog.save()
    await tts.close()
    await stt.close()
    return clog


# ─── Round-trip test ──────────────────────────────────────────────────────────

async def run_roundtrip_test(
    script: dict,
    category: str,
    model: str,
    dry_run: bool,
) -> CallLog:
    """
    For a real call this delegates to the FastAPI WebSocket bridge (server/app.py).
    In dry-run mode we simulate the full pipeline locally to validate the logic.
    """
    call_uuid = str(uuid.uuid4())[:8]
    clog = CallLog(
        call_uuid=call_uuid,
        script_id=script["id"],
        category=category,
        model=model,
        mode="roundtrip",
        dry_run=dry_run,
    )
    clog.mark(Stage.CALL_INITIATED)

    if not dry_run:
        provider = get_provider(dry_run=False)
        to_number = os.environ["PLIVO_TO_NUMBER"]
        result = provider.place_call(
            to=to_number,
            answer_url=(
                f"{WEBHOOK_BASE_URL}/webhooks/answer"
                f"?script_id={script['id']}&category={category}&model={model}&mode=roundtrip"
            ),
            hangup_url=f"{WEBHOOK_BASE_URL}/webhooks/hangup",
        )
        if result.error:
            clog.error = result.error
            clog.finalize()
            clog.save()
            return clog

        # Real-call latency is measured by the WebSocket bridge (server/app.py).
        # The log file will be written there.  We note the initiation here.
        console.print(f"  [green]Call placed — uuid={result.call_uuid}[/green]")
        console.print(f"  [dim]Latency will be logged by the server WebSocket bridge.[/dim]")
        clog.call_uuid = result.call_uuid
        clog.finalize()
        clog.save()
        return clog

    # Dry-run: simulate full pipeline
    clog.mark(Stage.CALL_ANSWERED)
    clog.mark(Stage.STREAM_OPENED)

    # Simulate caller speaking (the script text played as TTS)
    tts_caller = get_tts_client(dry_run=True, model="mock-caller")
    caller_audio: list[bytes] = []
    async for chunk in tts_caller.stream(script["text"]):
        caller_audio.append(chunk)
    clog.mark(Stage.SPEECH_END_DETECTED)

    # STT on the "caller" audio
    stt = get_stt_client(dry_run=True)
    async def _caller_audio():
        for c in caller_audio:
            yield c

    final_text = ""
    async for event in stt.transcribe_stream(_caller_audio()):
        if event.is_final:
            clog.mark(Stage.STT_FINAL_RECEIVED)
            final_text = event.text

    # TTS response
    tts_agent = get_tts_client(dry_run=True, model=model)
    clog.mark(Stage.TTS_REQUEST_SENT)
    async for chunk in tts_agent.stream(
        f"Acknowledged: {final_text or script['text']}",
        on_first_byte=lambda: (
            clog.mark(Stage.TTS_FIRST_AUDIO_BYTE),
            clog.mark(Stage.FIRST_AGENT_AUDIO_BYTE),
        ),
    ):
        pass
    clog.mark(Stage.TTS_STREAM_COMPLETE)
    clog.mark(Stage.CALL_HANGUP)

    clog.finalize()
    clog.save()
    await tts_caller.close()
    await stt.close()
    await tts_agent.close()
    return clog


# ─── Main runner ──────────────────────────────────────────────────────────────

async def main(args) -> None:
    if args.aggregate_only:
        from eval.aggregate_results import main as agg_main
        import sys
        sys.argv = ["aggregate_results.py"]
        agg_main()
        return

    categories = load_scripts()
    models = [SONIC_35]
    if args.both_models:
        models.append(SONIC_3)
    if args.model:
        models = [args.model]

    modes = [args.mode] if args.mode != "all" else ["tts", "stt", "roundtrip"]
    dry_run = args.dry_run

    if not dry_run:
        console.print(f"[bold red]REAL CALLS MODE — cap: {MAX_CALLS} calls per run[/bold red]")

    total_calls = 0
    results: list[CallLog] = []

    for model in models:
        for cat in categories:
            for script in cat["scripts"]:
                for mode in modes:
                    for rep in range(args.repeats):
                        if not dry_run and total_calls >= MAX_CALLS:
                            console.print(f"[red]Hit MAX_REAL_CALLS_PER_RUN={MAX_CALLS} — stopping.[/red]")
                            break

                        console.print(
                            f"[bold]{mode}[/bold] | {cat['id']} | {script['id']} | "
                            f"model={model} | rep={rep+1}/{args.repeats}"
                        )

                        try:
                            if mode == "tts":
                                clog = await run_tts_test(script, cat["id"], model, dry_run)
                            elif mode == "stt":
                                clog = await run_stt_test(script, cat["id"], model, dry_run)
                            else:
                                clog = await run_roundtrip_test(script, cat["id"], model, dry_run)

                            results.append(clog)
                            if clog.error:
                                console.print(f"  [red]ERROR: {clog.error}[/red]")
                            else:
                                console.print(f"  [green]OK[/green] e2e={clog.end_to_end_ms or 'N/A'} ms  tts_first={clog.tts_first_byte_ms or 'N/A'} ms")

                        except Exception as exc:
                            console.print(f"  [red]EXCEPTION: {exc}[/red]")
                            # Still record the failure
                            err_log = CallLog(
                                call_uuid=f"err-{total_calls}",
                                script_id=script["id"],
                                category=cat["id"],
                                model=model,
                                mode=mode,
                                dry_run=dry_run,
                                error=str(exc),
                            )
                            err_log.save()

                        if not dry_run:
                            total_calls += 1
                            # Small delay between real calls to avoid rate limits
                            await asyncio.sleep(2)

    console.print(f"\n[bold]Done.[/bold] {len(results)} calls completed.")
    console.print("Run [cyan]python -m eval.aggregate_results[/cyan] to generate results/results.md")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cartesia × Plivo eval runner")
    parser.add_argument("--dry-run", action="store_true", help="Run without placing real calls")
    parser.add_argument("--mode", choices=["tts", "stt", "roundtrip", "all"], default="all")
    parser.add_argument("--model", help="Override TTS model (e.g. sonic-2)")
    parser.add_argument("--both-models", action="store_true", help="Run Sonic-3.5 AND Sonic-3")
    parser.add_argument("--repeats", type=int, default=REPEATS_DEFAULT)
    parser.add_argument("--aggregate-only", action="store_true")
    args = parser.parse_args()

    asyncio.run(main(args))
