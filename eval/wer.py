"""
WER (Word Error Rate) scoring against reference transcripts.

Uses `jiwer` under the hood.  The scorer also checks whether non-verbal tags
(e.g. "[laughter]") leaked through as spoken text instead of being rendered
as audio, which is a key fidelity test for Cartesia's tag handling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
import logging

log = logging.getLogger(__name__)

# Cartesia's published clean-audio WER baseline (update from official leaderboard)
# Source: https://cartesia.ai/blog  (placeholder — confirm before citing)
CARTESIA_PUBLISHED_WER_BASELINE = 0.035   # 3.5% — update from their published figures

# Known non-verbal tags Cartesia Sonic supports
NONVERBAL_TAGS = re.compile(r"\[(laughter|sigh|breath|cough|applause)\]", re.IGNORECASE)


@dataclass
class WERResult:
    hypothesis: str        # STT output
    reference: str         # ground-truth transcript
    wer: float             # 0.0–1.0
    word_errors: int
    total_words: int
    delta_vs_baseline: Optional[float] = None   # wer - CARTESIA_PUBLISHED_WER_BASELINE
    # Non-verbal fidelity
    nonverbal_tags_in_input: list[str] = None
    nonverbal_leaked_in_hypothesis: bool = False
    leak_details: Optional[str] = None


def score(hypothesis: str, reference: str, nonverbal_tags: Optional[list[str]] = None) -> WERResult:
    """
    Compute WER and check for non-verbal tag leakage.

    Args:
        hypothesis:     STT output (what Ink-2 heard over the phone).
        reference:      The clean ground-truth text (sans tags).
        nonverbal_tags: List of tag strings that were present in the TTS input
                        but should NOT appear in the STT output.
    """
    try:
        import jiwer
    except ImportError:
        raise ImportError("Install jiwer: pip install jiwer")

    output = jiwer.process_words(reference, hypothesis)
    wer = jiwer.wer(reference, hypothesis)
    word_errors = output.substitutions + output.deletions + output.insertions
    total_words = len(reference.split())

    # Non-verbal tag leak detection
    leaked = False
    leak_details = None
    if nonverbal_tags:
        for tag in nonverbal_tags:
            # Strip brackets and check if the word appears in hypothesis
            tag_word = tag.strip("[]").lower()
            if re.search(rf"\b{re.escape(tag_word)}\b", hypothesis.lower()):
                leaked = True
                leak_details = f"Tag '{tag}' leaked as spoken word in hypothesis."
                log.warning("Non-verbal tag leak detected: %s", leak_details)
                break

        # Also catch raw tag format
        if not leaked and NONVERBAL_TAGS.search(hypothesis):
            leaked = True
            leak_details = f"Raw tag syntax found in hypothesis: {hypothesis!r}"

    return WERResult(
        hypothesis=hypothesis,
        reference=reference,
        wer=wer,
        word_errors=word_errors,
        total_words=total_words,
        delta_vs_baseline=wer - CARTESIA_PUBLISHED_WER_BASELINE,
        nonverbal_tags_in_input=nonverbal_tags or [],
        nonverbal_leaked_in_hypothesis=leaked,
        leak_details=leak_details,
    )
