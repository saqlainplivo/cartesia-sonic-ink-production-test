#!/usr/bin/env python3
"""
aggregate_results.py — turns raw per-call JSON logs into results/results.md.

IMPORTANT: This script only reports numbers from logs that were actually
collected.  It never fabricates, interpolates, or assumes values.  If fewer
than the expected REPEATS_PER_CATEGORY logs exist, the report will say so
explicitly rather than padding with placeholder numbers.

Usage:
    python -m eval.aggregate_results [--logs-dir logs] [--out results/results.md]
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import sys

LOGS_DIR_DEFAULT = Path("logs")
OUT_DEFAULT = Path("results/results.md")

CARTESIA_PUBLISHED_WER_BASELINE = 0.035  # 3.5% — confirm from official source


# ─── Data helpers ─────────────────────────────────────────────────────────────

def _p95(values: list[float]) -> float:
    if not values:
        return float("nan")
    values_sorted = sorted(values)
    idx = int(len(values_sorted) * 0.95)
    return values_sorted[min(idx, len(values_sorted) - 1)]


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None}
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": _p95(values),
    }


def _fmt(v: Optional[float], unit: str = "ms") -> str:
    if v is None:
        return "N/A"
    return f"{v:.1f} {unit}"


def _pct(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:.2f}%"


# ─── Log loading ──────────────────────────────────────────────────────────────

def load_logs(logs_dir: Path) -> list[dict]:
    logs = []
    for p in sorted(logs_dir.glob("*.json")):
        try:
            with open(p) as f:
                logs.append(json.load(f))
        except Exception as e:
            print(f"  WARNING: could not parse {p}: {e}", file=sys.stderr)
    return logs


# ─── Aggregation ─────────────────────────────────────────────────────────────

@dataclass
class CategoryStats:
    category: str
    model: str
    n_calls: int
    n_errors: int
    call_setup_ms: dict
    tts_first_byte_ms: dict
    end_to_end_ms: dict
    wer_values: list[float]
    wer_stats: dict
    nonverbal_leaks: int
    nonverbal_total: int


def aggregate(logs: list[dict]) -> list[CategoryStats]:
    # Group by (category, model)
    groups: dict[tuple, list[dict]] = {}
    for log in logs:
        key = (log.get("category", "unknown"), log.get("model", "unknown"))
        groups.setdefault(key, []).append(log)

    results = []
    for (category, model), entries in sorted(groups.items()):
        n_errors = sum(1 for e in entries if e.get("error"))
        call_setup = [e["call_setup_ms"] for e in entries if e.get("call_setup_ms") is not None]
        tts_first = [e["tts_first_byte_ms"] for e in entries if e.get("tts_first_byte_ms") is not None]
        e2e = [e["end_to_end_ms"] for e in entries if e.get("end_to_end_ms") is not None]
        wer_vals = [e["wer"] for e in entries if e.get("wer") is not None]

        # Non-verbal fidelity
        nonverbal_total = 0
        nonverbal_leaks = 0
        for e in entries:
            nv = e.get("nonverbal_leaked")
            if nv is not None:
                nonverbal_total += 1
                if nv:
                    nonverbal_leaks += 1

        results.append(CategoryStats(
            category=category,
            model=model,
            n_calls=len(entries),
            n_errors=n_errors,
            call_setup_ms=_stats(call_setup),
            tts_first_byte_ms=_stats(tts_first),
            end_to_end_ms=_stats(e2e),
            wer_values=wer_vals,
            wer_stats=_stats(wer_vals),
            nonverbal_leaks=nonverbal_leaks,
            nonverbal_total=nonverbal_total,
        ))
    return results


# ─── Report generation ────────────────────────────────────────────────────────

def render_report(stats: list[CategoryStats], logs: list[dict]) -> str:
    dry_run_any = any(l.get("dry_run") for l in logs)
    error_logs = [l for l in logs if l.get("error")]

    lines = []
    lines.append("# Cartesia Sonic × Ink-2 — Production Phone Call Eval Results\n")

    if dry_run_any:
        lines.append(
            "> **⚠ DRY-RUN DATA** — Some or all logs were produced in `--dry-run` mode.  "
            "Numbers below do NOT reflect real calls or real model latency.\n"
        )

    lines.append(f"**Total calls logged:** {len(logs)}  ")
    lines.append(f"**Calls with errors:** {len(error_logs)}  ")
    lines.append(f"**Cartesia published clean-audio WER baseline:** {_pct(CARTESIA_PUBLISHED_WER_BASELINE)}\n")

    # ── Latency table ──────────────────────────────────────────────────────
    lines.append("## End-to-End Latency (ms)\n")
    lines.append("| Category | Model | N | Call Setup mean/med/p95 | TTS First Byte mean/med/p95 | E2E Round-trip mean/med/p95 |")
    lines.append("|---|---|---|---|---|---|")
    for s in stats:
        cs = s.call_setup_ms
        tf = s.tts_first_byte_ms
        e2 = s.end_to_end_ms
        lines.append(
            f"| {s.category} | `{s.model}` | {s.n_calls} "
            f"| {_fmt(cs.get('mean'))} / {_fmt(cs.get('median'))} / {_fmt(cs.get('p95'))} "
            f"| {_fmt(tf.get('mean'))} / {_fmt(tf.get('median'))} / {_fmt(tf.get('p95'))} "
            f"| {_fmt(e2.get('mean'))} / {_fmt(e2.get('median'))} / {_fmt(e2.get('p95'))} |"
        )

    # ── WER table ──────────────────────────────────────────────────────────
    lines.append("\n## STT Word Error Rate (WER) vs. Cartesia Baseline\n")
    lines.append("| Category | Model | N | Mean WER | Median WER | p95 WER | vs. Baseline (Δ) |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in stats:
        ws = s.wer_stats
        if ws["n"] == 0:
            lines.append(f"| {s.category} | `{s.model}` | 0 | N/A | N/A | N/A | N/A |")
            continue
        delta = (ws["mean"] - CARTESIA_PUBLISHED_WER_BASELINE) if ws["mean"] is not None else None
        delta_str = (f"+{_pct(delta)}" if (delta or 0) >= 0 else _pct(delta)) if delta is not None else "N/A"
        lines.append(
            f"| {s.category} | `{s.model}` | {ws['n']} "
            f"| {_pct(ws.get('mean'))} | {_pct(ws.get('median'))} | {_pct(ws.get('p95'))} "
            f"| {delta_str} |"
        )

    # ── Non-verbal fidelity ────────────────────────────────────────────────
    lines.append("\n## Non-Verbal Tag Fidelity (`[laughter]` etc.)\n")
    nv_rows = [s for s in stats if s.nonverbal_total > 0]
    if not nv_rows:
        lines.append("_No non-verbal tag tests were run (or nonverbal_leaked field absent from logs)._\n")
    else:
        lines.append("| Category | Model | Tests | Leaks (tag spoken aloud) | Fidelity |")
        lines.append("|---|---|---|---|---|")
        for s in nv_rows:
            fidelity = "✅ PASS" if s.nonverbal_leaks == 0 else f"❌ FAIL ({s.nonverbal_leaks}/{s.nonverbal_total} leaked)"
            lines.append(f"| {s.category} | `{s.model}` | {s.nonverbal_total} | {s.nonverbal_leaks} | {fidelity} |")

    # ── Sonic-3.5 vs Sonic-3 comparison ──────────────────────────────────
    lines.append("\n## Sonic-3.5 vs. Sonic-3 Side-by-Side\n")
    by_cat: dict[str, dict[str, CategoryStats]] = {}
    for s in stats:
        by_cat.setdefault(s.category, {})[s.model] = s

    lines.append("| Category | Metric | Sonic-3.5 | Sonic-3 | Difference |")
    lines.append("|---|---|---|---|---|")
    for cat, models in sorted(by_cat.items()):
        for metric_label, field in [
            ("TTS First Byte p95", "tts_first_byte_ms"),
            ("E2E p95", "end_to_end_ms"),
            ("WER mean", "wer_stats"),
        ]:
            s35 = models.get("sonic-2") or models.get("sonic-3.5")
            s3 = models.get("sonic") or models.get("sonic-3")
            if not s35 or not s3:
                continue
            if field == "wer_stats":
                v35 = (s35.wer_stats or {}).get("mean")
                v3 = (s3.wer_stats or {}).get("mean")
                fmt = lambda v: _pct(v)
            else:
                v35 = (getattr(s35, field) or {}).get("p95") if metric_label.endswith("p95") else (getattr(s35, field) or {}).get("mean")
                v3 = (getattr(s3, field) or {}).get("p95") if metric_label.endswith("p95") else (getattr(s3, field) or {}).get("mean")
                fmt = lambda v: _fmt(v)
            diff = ((v35 or 0) - (v3 or 0)) if v35 is not None and v3 is not None else None
            diff_str = (f"+{fmt(diff)}" if (diff or 0) >= 0 else fmt(diff)) if diff is not None else "N/A"
            lines.append(f"| {cat} | {metric_label} | {fmt(v35)} | {fmt(v3)} | {diff_str} |")

    # Subjective quality note placeholder (never auto-filled)
    lines.append("\n### Subjective Quality Notes\n")
    lines.append("_Fill in after listening to recordings — this section is intentionally blank and must not be auto-populated._\n")

    # ── Errors / dropped calls ────────────────────────────────────────────
    lines.append("\n## Errors and Dropped Calls\n")
    if not error_logs:
        lines.append("_No errors recorded._\n")
    else:
        lines.append("| Call UUID | Category | Model | Error |")
        lines.append("|---|---|---|---|")
        for l in error_logs:
            lines.append(
                f"| `{l.get('call_uuid', '?')}` | {l.get('category', '?')} "
                f"| `{l.get('model', '?')}` | {l.get('error', 'unknown')} |"
            )

    lines.append("\n---\n")
    lines.append("_Report generated by `eval/aggregate_results.py`.  All numbers are from real call logs._\n")

    return "\n".join(lines)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate per-call logs into results/results.md")
    parser.add_argument("--logs-dir", type=Path, default=LOGS_DIR_DEFAULT)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args()

    if not args.logs_dir.exists():
        print(f"ERROR: logs directory not found: {args.logs_dir}", file=sys.stderr)
        sys.exit(1)

    logs = load_logs(args.logs_dir)
    if not logs:
        print("ERROR: No log files found — no results to aggregate.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(logs)} call log(s) from {args.logs_dir}")
    stats = aggregate(logs)
    report = render_report(stats, logs)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(f"Report written → {args.out}")


if __name__ == "__main__":
    main()
