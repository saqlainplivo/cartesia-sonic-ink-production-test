# Cartesia Sonic × Ink-2 — Production Phone Call Eval

> **Does Cartesia's sub-90ms latency claim hold up over a real phone call?**
>
> This project puts Cartesia's Sonic-3.5 TTS and Ink-2 STT through actual PSTN calls via the Plivo Voice API — with real G.711 codec compression, real network jitter, and real call-setup overhead — and measures what actually comes out the other end.

**Why this matters:** Cartesia's published latency and WER numbers come from clean-audio API tests. A browser test or local API call skips the one thing that matters most for voice AI in production: the phone network. G.711 µ-law encoding at 8 kHz squashes audio quality, introduces ~20ms of codec delay per hop, and can corrupt subtle audio cues like laughter or whispering. This eval measures the real delta.

---

## What this project measures

| Metric | How it's measured |
|---|---|
| **Call setup latency** | Time from `place_call()` to `call_answered` webhook — Plivo overhead, not Cartesia |
| **TTS first-byte latency** | Time from sending text to Cartesia until the first audio byte arrives |
| **End-to-end round-trip** | From end of caller speech → Ink-2 final transcript → Sonic TTS → first audio byte back in the call |
| **STT Word Error Rate (WER)** | Ink-2's transcript vs. reference text, scored with `jiwer`, compared to Cartesia's published clean-audio WER |
| **Non-verbal tag fidelity** | Does `[laughter]` render as audio or leak as the spoken word "laughter" after codec compression? |
| **Sonic-3.5 vs Sonic-3** | Both models run through the identical call pipeline — latency and WER side by side |

All stats are **mean / median / p95** across 5 repeated calls per script category. No numbers are hand-written; everything in `results/results.md` is generated from real call logs.

---

## Six test script categories

The same six categories used in the original Cartesia community review:

| Category | What it tests |
|---|---|
| **Neutral speech** | Baseline fluency and WER reference |
| **Emotional / Expressive** | Does excitement or frustration survive codec compression? |
| **Apology / Empathy** | Warmth and soft tone retention |
| **Laughter / Non-verbal tags** | `[laughter]` must render as audio, not spoken text — critical fidelity test |
| **Structured data** | Phone numbers, dates, dollar amounts — digit accuracy in WER |
| **Condolence / Sensitive** | Low-energy, careful delivery — hardest register to preserve |

---

## Architecture

```
Your phone (PSTN)
     │  G.711 µ-law 8kHz
     ▼
 Plivo Voice API
     │  WebSocket (<Stream> verb)
     ▼
 FastAPI server  (server/app.py)
     │
     ├─► Cartesia Ink-2 STT  ──► transcript + timestamps
     │
     └─► Cartesia Sonic TTS  ──► PCM audio ──► µ-law ──► WebSocket ──► Plivo ──► caller
```

The server sits between Plivo's audio WebSocket and Cartesia's streaming APIs. It timestamps every stage so call-setup overhead, model latency, and network latency are reported separately — never lumped together.

---

## Project layout

```
cartesia-sonic-ink-production-test/
├── README.md
├── LICENSE                      MIT
├── .env.example                 Copy to .env and fill in your credentials
├── requirements.txt
├── run_eval.py                  ← Start here. Runs all test modes.
│
├── telephony/
│   └── provider.py              Plivo Voice API client (Twilio-swappable interface)
│
├── server/
│   ├── app.py                   FastAPI server: Plivo webhooks + WebSocket audio bridge
│   ├── cartesia_tts.py          Sonic-3.5 / Sonic-3 streaming TTS (model switchable)
│   ├── cartesia_stt.py          Ink-2 streaming STT
│   └── latency_logger.py        Timestamps every pipeline stage → JSON log per call
│
├── test_scripts/
│   └── scripts.json             6 categories × 2 scripts each
│
├── eval/
│   ├── wer.py                   WER scoring + non-verbal tag leak detection
│   └── aggregate_results.py     JSON logs → results/results.md (mean/median/p95)
│
└── results/
    └── .gitkeep                 results.md is git-ignored — only real runs produce it
```

---

## Setup (step by step)

### Step 1 — What you need before starting

You'll need accounts and credentials from two services:

**Plivo** (telephony — makes/receives the phone calls)
- Sign up at [console.plivo.com](https://console.plivo.com)
- From the dashboard, copy your **Auth ID** and **Auth Token**
- Buy a voice-capable phone number: Numbers → Buy a Number → filter by "Voice"
- You'll also need a second phone number to *receive* the test calls (your own mobile works)

**Cartesia** (AI models — TTS and STT)
- Sign up at [cartesia.ai](https://cartesia.ai)
- Get your **API key** from the dashboard
- Pick a **voice ID** from [app.cartesia.ai/voices](https://app.cartesia.ai/voices) — copy the ID shown under any voice

### Step 2 — Clone and install

```bash
git clone https://github.com/saqlainplivo/cartesia-sonic-ink-production-test.git
cd cartesia-sonic-ink-production-test

# Create a virtual environment (keeps dependencies isolated)
python -m venv .venv
source .venv/bin/activate        # On Windows: .venv\Scripts\activate

# Install everything
pip install -r requirements.txt
```

> **Python version:** Python 3.10–3.12 recommended. Python 3.13+ users: run `pip install audioop-lts` after the above (the `audioop` module was moved out of stdlib in 3.13).

### Step 3 — Configure credentials

```bash
cp .env.example .env
```

Open `.env` in any text editor and fill in your values:

```env
# Plivo (from console.plivo.com dashboard)
PLIVO_AUTH_ID=MAxxxxxxxxxxxxxxxxxxxxxxxx
PLIVO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
PLIVO_FROM_NUMBER=+1XXXXXXXXXX        # The Plivo number you bought
PLIVO_TO_NUMBER=+1XXXXXXXXXX          # Your phone — will receive test calls

# Cartesia (from cartesia.ai dashboard)
CARTESIA_API_KEY=sk-xxxxxxxxxxxxxxxx
CARTESIA_VOICE_ID=a0e99841-438c-4a64-b679-ae501e7d6091   # Replace with your chosen voice

# Leave these as-is unless Cartesia changes their model IDs
CARTESIA_MODEL_SONIC_35=sonic-2
CARTESIA_MODEL_SONIC_3=sonic
CARTESIA_MODEL_INK_2=ink-2

# Webhook URL — see Step 4
WEBHOOK_BASE_URL=https://REPLACE-THIS.ngrok-free.app

# Safety cap — real calls cost money on both Plivo and Cartesia
MAX_REAL_CALLS_PER_RUN=10
```

### Step 4 — Expose a public webhook URL (required for real calls)

Plivo needs to send HTTP webhooks to your server when a call connects. In development, use **ngrok** to create a temporary public URL:

```bash
# Install ngrok from https://ngrok.com/download, then:
ngrok http 5000
```

ngrok will print something like:
```
Forwarding  https://abc123.ngrok-free.app -> http://localhost:5000
```

Copy that `https://` URL into your `.env` as `WEBHOOK_BASE_URL`.

> For production deployments, host the FastAPI server on any cloud provider and use your actual domain.

### Step 5 — Start the server

```bash
# In one terminal (keep this running):
uvicorn server.app:app --port 5000 --reload
```

Check it's working:
```bash
curl http://localhost:5000/health
# Should return: {"status":"ok","dry_run":false}
```

---

## Running the eval

### Try the dry-run first (free, no calls placed)

The dry-run exercises the complete pipeline logic — all the same code paths, timing, WER scoring — using mock audio instead of real calls. **Run this before spending any money.**

```bash
python run_eval.py --dry-run --mode all --both-models --repeats 5
```

You'll see output like:
```
tts | neutral | neutral_1 | model=sonic-2 | rep=1/5
  OK e2e=N/A ms  tts_first=21.1 ms
roundtrip | neutral | neutral_1 | model=sonic-2 | rep=1/5
  OK e2e=422.3 ms  tts_first=21.2 ms
...
Done. 360 calls completed.
```

Then generate the report:
```bash
python -m eval.aggregate_results
# Writes → results/results.md
```

> **Note:** Dry-run numbers are mock-pipeline timing (local asyncio delays), not real model latency. The report will say so clearly. Only real calls produce real numbers.

### Real calls — start small

```bash
# TTS only, 3 repeats, Sonic-3.5 model — places up to 6 real calls
python run_eval.py --mode tts --repeats 3 --model sonic-2
```

```bash
# STT + WER measurement
python run_eval.py --mode stt --repeats 5 --model sonic-2
```

```bash
# Both models side-by-side (Sonic-3.5 and Sonic-3)
python run_eval.py --mode all --both-models --repeats 5
```

> The `MAX_REAL_CALLS_PER_RUN` cap in `.env` hard-stops the runner. Increase it deliberately when you're ready for a full run.

### Generate the final report

```bash
python -m eval.aggregate_results
```

Opens `results/results.md` — contains mean/median/p95 latency per category, WER vs. Cartesia's published baseline, non-verbal tag fidelity results, Sonic-3.5 vs Sonic-3 comparison, and a full error log.

---

## Test modes explained

| Mode | `--mode` flag | What happens |
|---|---|---|
| **TTS-focused** | `tts` | Places an outbound call, streams Sonic TTS audio into it via Plivo, records the call. Checks whether `[laughter]` survives as audio or leaks as text. |
| **STT-focused** | `stt` | Generates TTS audio from a known script, feeds it through Ink-2, computes WER against the reference text. |
| **End-to-end round-trip** | `roundtrip` | Full voice agent: caller speaks → Ink-2 transcribes → canned response → Sonic TTS streams back. Measures true speech-end to first-audio-byte latency. |
| **All** | `all` | Runs all three modes. |

---

## Reading the results

After a real run, `results/results.md` will contain:

- **Latency table** — call setup / TTS first byte / end-to-end, each as mean/median/p95 in milliseconds
- **WER table** — Ink-2's accuracy per category, vs. Cartesia's published 3.5% clean-audio baseline, with the delta (Δ) called out
- **Non-verbal fidelity** — PASS/FAIL per category (did `[laughter]` stay as audio?)
- **Sonic-3.5 vs Sonic-3** — side-by-side latency and WER
- **Error log** — every dropped call, timeout, or API failure listed explicitly

---

## Guardrails built in

- **No fabricated results.** `results/results.md` only ever contains numbers from real logs. If a test hasn't been run, the report says so.
- **Call cap.** `MAX_REAL_CALLS_PER_RUN` prevents runaway spending.
- **Credentials stay local.** `.env` is in `.gitignore` and never committed.
- **Errors are visible.** Failed calls appear in the report; exceptions are logged to console, not swallowed.
- **Dry-run is clearly marked.** Dry-run logs are flagged in the report with a warning banner.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `PLIVO_AUTH_ID not set` | Make sure you ran `cp .env.example .env` and filled in the values |
| Plivo webhook returns 404 | Check ngrok is running and `WEBHOOK_BASE_URL` in `.env` matches the ngrok URL exactly |
| `ModuleNotFoundError: cartesia` | Run `pip install -r requirements.txt` inside your virtual environment |
| `audioop` not found (Python 3.13) | Run `pip install audioop-lts` |
| WER is 100% | Ink-2 returned no transcript — verify `CARTESIA_API_KEY` and `CARTESIA_MODEL_INK_2=ink-2` |
| `[laughter]` leaks as spoken text | Check Cartesia's current docs for the correct non-verbal tag syntax — it may have changed |
| Call connects but no audio | Confirm `WEBHOOK_BASE_URL` uses `https://`, not `http://` — Plivo requires HTTPS for webhooks |

---

## Swapping to Twilio

The `telephony/provider.py` file defines a `TelephonyProvider` base class. To add Twilio:
1. Subclass `TelephonyProvider` in the same file
2. Implement `place_call`, `hangup_call`, `get_call_status`, and `build_stream_xml` using Twilio's `<Stream>` TwiML
3. Update `get_provider()` to return your new class

The server, eval pipeline, and latency logger are all provider-agnostic.

---

## License

MIT — see [LICENSE](LICENSE).
