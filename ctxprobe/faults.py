"""Wear simulation: every corruption here is something real harnesses do.

TRAPCPU's snapshot layer shipped fault injectors because a persistence layer
whose failure modes have never been exercised is a persistence layer that
fails silently in production. Same policy here. Each function degrades a
transcript the way a specific real mechanism degrades a context window, so a
scan of the output demonstrates — and tests assert — that the instrument
detects that mechanism.

* :func:`simulate_eviction`     — context eviction: sector lines vanish, no gap
* :func:`simulate_bitrot`       — in-place mangling: reflow, OCR, typo repair
* :func:`simulate_rewrite`      — a model "tidies" a sector and fixes its CRC;
                                  the one only seed regeneration can catch
* :func:`simulate_truncation`   — the oldest fraction of the window falls off
* :func:`simulate_header_loss`  — summarization keeps the "gist" and drops
                                  bookkeeping lines; used to prove that losing
                                  the map makes eviction silent

All functions are deterministic given ``rng`` (default ``random.Random(0)``)
and pure: they return new text, never mutate files.
"""

import random
import re

from .frame import crc16

_SECTOR_RE = re.compile(r"^@([0-9A-Fa-f]{1,2})\s+([0-9A-Fa-f]+)\s+([0-9A-Fa-f]{4})\s*$")


def _sector_indexes(lines):
    return [index for index, line in enumerate(lines)
            if _SECTOR_RE.match(line.strip())]


def simulate_eviction(text, fraction=0.25, rng=None):
    """Delete a fraction of the sector lines. Nothing marks the gap."""
    rng = rng or random.Random(0)
    lines = text.split("\n")
    candidates = _sector_indexes(lines)
    drop = int(len(candidates) * fraction)
    doomed = set(rng.sample(candidates, drop)) if drop else set()
    return "\n".join(line for index, line in enumerate(lines)
                     if index not in doomed)


def simulate_bitrot(text, flips=1, rng=None):
    """Mangle hex digits inside sector lines; the sector CRC catches it."""
    rng = rng or random.Random(0)
    lines = text.split("\n")
    candidates = _sector_indexes(lines)
    if not candidates:
        return text

    for _ in range(flips):
        index = rng.choice(candidates)
        match = _SECTOR_RE.match(lines[index].strip())
        address, body, checksum = match.groups()
        position = rng.randrange(len(body))
        digit = body[position]
        replacement = "0" if digit != "0" else "F"
        body = body[:position] + replacement + body[position + 1:]
        lines[index] = "@%s %s %s" % (address, body, checksum)

    return "\n".join(lines)


def simulate_rewrite(text, rewrites=1, rng=None):
    """Alter a sector's payload and recompute its CRC to match.

    This is the "helpful model" failure: the bytes are wrong, the arithmetic
    is right, and every checksum on earth says the sector is fine. Only
    comparing against the seed-regenerated payload detects it.
    """
    rng = rng or random.Random(0)
    lines = text.split("\n")
    candidates = _sector_indexes(lines)
    if not candidates:
        return text

    for _ in range(rewrites):
        index = rng.choice(candidates)
        match = _SECTOR_RE.match(lines[index].strip())
        address, body, _ = match.groups()
        blob = bytearray(bytes.fromhex(body))
        position = rng.randrange(len(blob))
        blob[position] ^= 0xFF
        lines[index] = "@%s %s %04X" % (
            address, bytes(blob).hex().upper(), crc16(bytes(blob)))

    return "\n".join(lines)


def simulate_truncation(text, keep=0.5):
    """Keep only the newest ``keep`` fraction of the lines.

    Context windows shed from the far end: the oldest text goes first. A probe
    emitted early enough disappears wholesale — the loss only a PREV chain or
    an ``expect`` list can see.
    """
    lines = text.split("\n")
    start = len(lines) - max(0, int(len(lines) * keep))
    return "\n".join(lines[start:])


def simulate_header_loss(text, keys=("MAP", "SECTORS"), rng=None):
    """Drop named header lines from every frame, keeping everything else.

    This is what summarization does: the data "looks preserved" while the
    bookkeeping evaporates. Scanning the result proves the negative space —
    without MAP and SECTORS, evicted sectors stop being reported at all.
    """
    del rng  # deterministic by construction; parameter kept for symmetry
    doomed = tuple("%s:" % key.upper() for key in keys)
    return "\n".join(line for line in text.split("\n")
                     if not line.strip().upper().startswith(doomed))
