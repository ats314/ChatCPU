"""ctxprobe: an integrity instrument for context windows.

badblocks for transcripts. TRAPCPU stored a machine's memory in the chat
transcript and discovered that an attention window is a storage medium with
a failure mode no disk has: it loses data *silently* — eviction, summarization,
and "helpful" reflow leave no gap where the bytes used to be. This package is
that discovery extracted into a standalone instrument, with the CPU removed.

A *probe* is a frame of pseudo-random sentinel data, generated from a seed,
written out as independently checksummed sectors plus an allocation map. You
emit probes into any long-running conversation. Later — after compaction,
summarization, model rewrites, whatever the harness did — you scan whatever
text survives, and the report says exactly what happened to every sector:

    OK          survived byte-for-byte (regenerated from seed and compared)
    UNVERIFIED  checksum passes but the seed was lost, so bytes are unproven
    REWRITTEN   checksum passes but bytes differ: something recomputed the CRC
    CORRUPTED   checksum fails: bytes were mangled in place
    MANGLED     the sector line no longer parses at all
    EVICTED     the allocation map promises a sector the text no longer has

Because payloads regenerate from the seed, the scanner needs no stored copy of
the data — the probe frame is self-describing, and REWRITTEN is detectable at
all, which a checksum alone can never do. Because each probe's header lists the
ids of the probes emitted before it, a probe whose *entire frame* was evicted
is still detected by any newer probe that survived. Because every frame records
where in the text it was found, loss becomes a curve over context depth rather
than a single number.

Standard library only. Python 3.8+. No dependency on trapcpu.
"""

from .frame import (
    FORMAT_VERSION,
    PROBE_BEGIN,
    PROBE_END,
    SECTOR_BYTES,
    crc16,
    derive_probe_id,
    emit,
    sector_payload,
)
from .scan import (
    ProbeReport,
    ScanReport,
    SectorState,
    Status,
    find_probes,
    scan,
)
from .faults import (
    simulate_bitrot,
    simulate_eviction,
    simulate_header_loss,
    simulate_rewrite,
    simulate_truncation,
)

__all__ = [
    "FORMAT_VERSION",
    "PROBE_BEGIN",
    "PROBE_END",
    "SECTOR_BYTES",
    "crc16",
    "derive_probe_id",
    "emit",
    "sector_payload",
    "ProbeReport",
    "ScanReport",
    "SectorState",
    "Status",
    "find_probes",
    "scan",
    "simulate_bitrot",
    "simulate_eviction",
    "simulate_header_loss",
    "simulate_rewrite",
    "simulate_truncation",
]
