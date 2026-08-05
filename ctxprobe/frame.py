"""Probe generation: deterministic sentinel data, framed for survival.

A probe frame looks like this::

    === CTXPROBE/1 SENTINEL ===
    ID: 3F9A0C11
    SEED: 9E3779B97F4A7C15
    LABEL: after-refactor
    SECTORS: 16
    SECBYTES: 32
    PREV: A1B2C3D4,55E1D200
    MAP: 00,01,02,...,0F
    METACRC: 7C31
    DATA>>>
    @00 4F52...  9B14
    ...
    <<<DATA
    === CTXPROBE END ===

Every design choice is a lesson inherited from TRAPCPU's snapshot layer:

* Sector payloads are pseudo-random bytes generated from ``SEED`` by a
  fixed-in-this-file PRNG, so a scanner can regenerate the expected contents
  from the header alone. No stored copy, and — unlike a checksum — the
  comparison catches an adversary (or a helpful model) that rewrites a sector
  *and* fixes its CRC.
* Each sector line carries its own address and CRC, because transcript loss is
  line-granular: one evicted line must not take the frame down with it.
* ``MAP`` is the allocation table. Without it, an evicted sector is simply
  absent and nothing notices; with it, the scanner names the missing indices.
* ``METACRC`` covers only the header lines, separately from the sector CRCs,
  so "the metadata is trustworthy" and "the data is intact" are independent
  verdicts.
* ``PREV`` chains probes together. A frame that vanishes wholesale is invisible
  to its own map — but not to the map of every probe emitted after it.
"""

import os

FORMAT_VERSION = 1
PROBE_BEGIN = "=== CTXPROBE/%d SENTINEL ===" % FORMAT_VERSION
PROBE_END = "=== CTXPROBE END ==="
DATA_OPEN, DATA_CLOSE = "DATA>>>", "<<<DATA"

SECTOR_BYTES = 32
MAX_SECTORS = 256
MAX_PREV = 16

_M64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15


def crc16(data, init=0xFFFF):
    """CRC-16/CCITT-FALSE, bit-identical to trapcpu's, reimplemented so this
    package stands alone."""
    crc = init
    for byte in data:
        crc ^= (byte << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _mix(value):
    """SplitMix64 finalizer. Defined here, not borrowed from ``random``,
    because the payload contract must never move between Python versions."""
    value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9 & _M64
    value = (value ^ (value >> 27)) * 0x94D049BB133111EB & _M64
    return value ^ (value >> 31)


def sector_payload(seed, index, length=SECTOR_BYTES):
    """The expected contents of sector ``index`` for a probe with ``seed``."""
    out = bytearray()
    state = (seed ^ ((index + 1) * _GOLDEN)) & _M64
    while len(out) < length:
        state = (state + _GOLDEN) & _M64
        out.extend(_mix(state).to_bytes(8, "big"))
    return bytes(out[:length])


def derive_probe_id(seed):
    """Eight hex characters, a stable function of the seed."""
    return "%08X" % (_mix((seed ^ 0xC7) & _M64) >> 32)


def _sector_line(index, blob):
    return "@%02X %s %04X" % (index, blob.hex().upper(), crc16(blob))


def emit(seed=None, sectors=16, sector_bytes=SECTOR_BYTES, probe_id=None,
         prev=(), label=None):
    """Render one probe frame as text. Returns ``(frame, seed, probe_id)``.

    ``seed`` is a 64-bit integer; ``None`` draws one from ``os.urandom``. The
    caller does not need to remember anything: the frame itself carries the
    seed, and the scanner regenerates the payload from it.
    """
    if seed is None:
        seed = int.from_bytes(os.urandom(8), "big")
    seed &= _M64

    if not 1 <= sectors <= MAX_SECTORS:
        raise ValueError("sectors must be 1..%d" % MAX_SECTORS)
    if not 1 <= sector_bytes <= 64:
        raise ValueError("sector_bytes must be 1..64")

    if probe_id is None:
        probe_id = derive_probe_id(seed)

    prev = [token.strip().upper() for token in prev if token.strip()]
    prev = prev[-MAX_PREV:]

    meta = [
        "ID: %s" % probe_id,
        "SEED: %016X" % seed,
    ]
    if label:
        meta.append("LABEL: %s" % " ".join(str(label).split()))
    meta.append("SECTORS: %d" % sectors)
    meta.append("SECBYTES: %d" % sector_bytes)
    if prev:
        meta.append("PREV: " + ",".join(prev))
    meta.append("MAP: " + ",".join("%02X" % i for i in range(sectors)))

    metacrc = crc16("\n".join(meta).encode("utf-8"))

    lines = [PROBE_BEGIN]
    lines.extend(meta)
    lines.append("METACRC: %04X" % metacrc)
    lines.append(DATA_OPEN)
    lines.extend(
        _sector_line(index, sector_payload(seed, index, sector_bytes))
        for index in range(sectors)
    )
    lines.append(DATA_CLOSE)
    lines.append(PROBE_END)

    return "\n".join(lines), seed, probe_id
