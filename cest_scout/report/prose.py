"""Keep free text readable: no report section is a single block of more than ~200 words.

Agent prose (rationales, critiques, caveats) arrives as one long string. It is split here into
short pieces at the markers the pipeline itself inserts (" | " between merged caveats, "[Tag]"
prefixes on deterministic notes) and at sentence boundaries, then grouped into bullets of at
most `max_words` words each. Nothing is summarized or dropped; the text is only re-flowed.
"""
from __future__ import annotations

import re

MAX_BLOCK_WORDS = 200      # above this a text is never shown as one paragraph
MAX_BULLET_WORDS = 60      # each bullet stays this short

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[(\"'])")
_TAG_START = re.compile(r"\s+(?=\[[A-Z][A-Za-z -]{2,40}(?:\]|:))")


def word_count(text: str) -> int:
    return len((text or "").split())


def segments(text: str) -> list[str]:
    """Pieces in reading order: split at ' | ', at '[Tag]' markers, then into sentences."""
    out: list[str] = []
    for part in re.split(r"\s*\|\s*", text or ""):
        for tagged in _TAG_START.split(part):
            for sent in _SENTENCE_END.split(tagged):
                sent = sent.strip()
                if sent:
                    out.append(sent)
    return out


def _split_long(seg: str, max_words: int) -> list[str]:
    """A 'sentence' longer than max_words (reference provenance is one long clause chain with
    semicolons and no full stops) is split at '; ' and ' -- ' so it can still be re-flowed."""
    if word_count(seg) <= max_words:
        return [seg]
    parts, depth, cur, i = [], 0, [], 0
    while i < len(seg):
        ch = seg[i]
        depth += ch in "([" ; depth -= ch in ")]"
        if depth <= 0 and (seg.startswith("; ", i) or seg.startswith(" -- ", i)):
            parts.append("".join(cur).strip()); cur = []
            i += 2 if seg.startswith("; ", i) else 4
            continue
        cur.append(ch); i += 1
    parts.append("".join(cur).strip())
    parts = [x for x in parts if x]
    return parts if len(parts) > 1 else [seg]


def bullets(text: str, max_words: int = MAX_BULLET_WORDS) -> list[str]:
    """Sentences grouped into bullets of at most max_words words (a single longer clause is
    left whole rather than cut mid-clause)."""
    out: list[str] = []
    current: list[str] = []
    n = 0
    for seg in (piece for s in segments(text) for piece in _split_long(s, max_words)):
        w = word_count(seg)
        if current and n + w > max_words:
            out.append(" ".join(current))
            current, n = [], 0
        current.append(seg)
        n += w
    if current:
        out.append(" ".join(current))
    return out


def is_long(text: str, max_words: int = MAX_BLOCK_WORDS) -> bool:
    return word_count(text) > max_words
