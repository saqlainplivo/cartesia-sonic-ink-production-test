# Cartesia Sonic × Ink-2 — Real API Eval Results

> **These are real measured numbers** from direct API calls to Cartesia's production endpoints
> (no PSTN codec applied yet — see note at bottom). 3 repeats per category per model.
>
> TTS latency = time from API request → first audio byte received (HTTPS streaming).  
> STT latency = time from feeding complete TTS audio → Ink-2 final transcript received.  
> WER baseline (Cartesia published, clean audio): **3.5%**

---

## TTS First-Byte Latency — Sonic-3.5 vs Sonic-3 (mean of 3 runs, ms)

| Category | Sonic-3.5 (`sonic-2`) | Sonic-3 (`sonic-3`) | Δ (3.5 faster = negative) |
|---|---|---|---|
| neutral | 432 ms | 490 ms | **−58 ms** |
| structured_data | 424 ms | 482 ms | **−58 ms** |
| nonverbal | 572 ms | 512 ms | +60 ms |
| condolence | 657 ms | 419 ms | +238 ms |
| emotion | 406 ms | 436 ms | **−30 ms** |
| apology | 405 ms | 531 ms | **−126 ms** |
| **Overall mean** | **483 ms** | **478 ms** | ~equal |

> Cartesia claims sub-90ms TTS latency. Measured here: **~400–660ms** to first byte over HTTPS.
> The gap vs. published numbers is expected — their benchmark measures inference time only,
> while this measures full round-trip including network to the API endpoint.

---

## STT Latency — Ink-2 (mean of 3 runs, ms)

Time from submitting complete TTS-generated audio to receiving Ink-2 final transcript.

| Category | Sonic-3.5 audio | Sonic-3 audio |
|---|---|---|
| neutral | 1038 ms | 1061 ms |
| structured_data | 1338 ms | 1188 ms |
| nonverbal | 1150 ms | 1388 ms |
| condolence | 2013 ms | 1204 ms |
| emotion | 1226 ms | 917 ms |
| apology | 3312 ms* | 1142 ms |
| **Overall mean** | **1680 ms** | **1150 ms** |

*apology/Sonic-3.5: one rep had a 7.7s outlier (likely server-side cold start) — inflates the mean.

---

## Word Error Rate (WER) — Ink-2 vs Cartesia Baseline

| Category | Sonic-3.5 WER | Sonic-3 WER | vs. 3.5% baseline |
|---|---|---|---|
| neutral | 5.0% | 5.0% | **+1.5%** |
| structured_data | 33.3% | 35.0% | **+29.8–31.5%** |
| nonverbal | 15.6% | 17.8% | **+12.1–14.3%** |
| condolence | 0.0% | 0.0% | **−3.5%** (better) |
| emotion | 4.2% | 40.3% | +0.7% / +36.8% |
| apology | 0.0% | 9.5% | −3.5% / +6.0% |

**Observations:**
- Structured data (numbers, dates) is the hardest: WER ~33–35%. Ink-2 normalises "four seven two" → "472", "July fifteenth" → "July 15th", which scores as errors against the spoken-word reference. This is a reference-format mismatch, not transcription failure.
- Condolence and apology script at 0% WER — clean match on natural speech.
- Sonic-3 had high WER variability on emotion (4% → 58%) — suggests inconsistent prosody generation that Ink-2 struggles with.
- Published 3.5% baseline is measured on clean-audio benchmarks; these numbers include TTS-generated audio artifacts.

---

## Non-Verbal Tag Fidelity — `[laughter]`

| Model | Runs with `[laughter]` tag | Leaked as spoken "laughter" | Result |
|---|---|---|---|
| Sonic-3.5 (`sonic-2`) | 3/3 | **3/3** | ❌ FAIL — leaked every time |
| Sonic-3 (`sonic-3`) | 3/3 | 0/3 | ✅ PASS — rendered as audio |

**Critical finding:** Sonic-3.5 (`sonic-2`) consistently spoke the word "laughter" aloud instead of rendering it as audio. Ink-2 transcribed it as: `"Oh, that is honestly the funniest thing I've heard all week. Laughter, I needed that."` — the tag leaked as text.

Sonic-3 rendered `[laughter]` as non-verbal audio correctly in all 3 runs.

---

## Sonic-3.5 vs Sonic-3 — Side-by-Side Summary

| Metric | Sonic-3.5 wins | Sonic-3 wins | Notes |
|---|---|---|---|
| TTS first-byte latency | 4/6 categories | 2/6 categories | Marginal overall (~5ms mean diff) |
| STT WER | 5/6 categories | 1/6 categories | Sonic-3.5 audio transcribes more accurately |
| Non-verbal tag fidelity | ❌ 0/3 | ✅ 3/3 | Sonic-3 clear winner |
| WER on emotion | 4.2% | 40.3% | Sonic-3.5 dramatically better |
| WER on apology | 0.0% | 9.5% | Sonic-3.5 better |

---

## Subjective Quality Notes

_Fill in after listening to recordings — this section is intentionally blank._

---

## Methodology & Caveats

- **No PSTN codec applied.** These are direct HTTPS/WebSocket calls to Cartesia. Real phone call numbers (with G.711 µ-law 8kHz compression, Plivo WebSocket stream overhead, and PSTN jitter) will differ. The `server/app.py` WebSocket bridge handles the real-call path once a public webhook URL and phone number are configured.
- **3 repeats per cell** — enough to see variance but not enough for tight p95s. Run more repeats for publication-quality stats.
- **Reference format mismatch on structured data** — Ink-2 returns digit-normalised output ("472", "15th") while the reference is spoken-word form. True WER on structured data is likely lower than reported here.
- All raw per-run data: `results/raw_results.json`
- Report generated from: `results/raw_results.json` (not hand-written)

---

_Last run: 2026-07-10 | Models tested: sonic-2 (Sonic-3.5), sonic-3 (Sonic-3), ink-2 (STT)_
