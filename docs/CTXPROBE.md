# CTXPROBE

**badblocks for transcripts.**

Every serious agent system now runs on long conversations that get compacted,
summarized, and evicted — and nobody can measure what gets lost. Context
degradation is silent: the model doesn't know a piece of its memory vanished
in summarization, the harness doesn't report it, and the user finds out when
the agent confidently acts on state that is no longer there. Storage
engineering solved this class of problem decades ago — CRCs, allocation
tables, surface scans. For attention windows, the storage medium the agent
economy actually runs on, there has been no instrument.

This is the instrument. It came out of [TRAPCPU](TRAPCPU.md), whose
persistence layer stored a CPU's memory in the chat transcript and had to
confront the medium honestly: sectors with individual CRCs, an allocation map,
and a test proving that without the map, eviction is *silent*. `ctxprobe` is
that layer extracted, with the CPU removed and three new capabilities added
that a machine snapshot never needed.

Standard library only. Python 3.8+. No dependency on `trapcpu`.

```console
$ python3 -m ctxprobe demo --seed 7
--- pristine transcript: 119 lines, 3 probes ---
CTXPROBE SCAN: 3 probe(s), 36 sector(s): 36 ok

--- after truncation + eviction + bitrot + a CRC-consistent rewrite ---
CTXPROBE SCAN: 2 probe(s), 1 vanished, 24 sector(s): 17 ok, 1 rewritten, 2 corrupted, 4 evicted
  probe 759F73A6 @line 10 DEGRADED [.~__......._] intact=256B lost=128B (depth 10%)
      sector @01 REWRITTEN: payload differs from seed regeneration but CRC verifies
      sector @02 EVICTED: missing from transcript (evicted)
      ...
  vanished: probe 1A16633A (referenced by 2 surviving probe(s), no frame found)
```

## How it works

A **probe** is a frame of pseudo-random sentinel data. You emit probes into
whatever medium you want to measure — a running conversation, a context that
is about to be compacted, a transcript headed into a summarizer — and later
scan whatever text survives:

```console
$ python3 -m ctxprobe emit --label before-compaction >> transcript.txt
$ # ... time passes, the harness does what harnesses do ...
$ python3 -m ctxprobe scan transcript.txt
$ python3 -m ctxprobe scan transcript.txt --json   # for charting
```

The frame is self-describing. Sector payloads are generated from a 64-bit
seed carried in the frame's own header, so the scanner regenerates the
expected bytes and compares — no stored copy, no database, no state outside
the medium being measured.

```text
=== CTXPROBE/1 SENTINEL ===
ID: 3F9A0C11
SEED: 9E3779B97F4A7C15
SECTORS: 16
SECBYTES: 32
PREV: A1B2C3D4,55E1D200
MAP: 00,01,02,...,0F
METACRC: 7C31
DATA>>>
@00 F00A249A...  8BED
...
<<<DATA
=== CTXPROBE END ===
```

## The failure taxonomy

The point is not a loss percentage. The point is that each status names a
**different physical failure of the medium**, because conflating them is how
loss stays invisible:

| Status | What happened to the bytes | Caught by |
| --- | --- | --- |
| `OK` | survived byte-for-byte | seed regeneration |
| `UNVERIFIED` | CRC passes but the seed was lost; survival unproven | honesty |
| `REWRITTEN` | payload altered *and* CRC recomputed to match | seed only |
| `CORRUPTED` | mangled in place; CRC fails | sector CRC |
| `MANGLED` | line no longer parses as a sector | parser |
| `EVICTED` | line is *gone*; no damage, no gap, just absence | the map only |
| vanished | an entire frame is gone | the PREV chain only |

Three of these deserve emphasis, because each is invisible to the layer below
it:

**`REWRITTEN` is invisible to checksums.** A model that "tidies" data — or any
process that rewrites and re-derives — produces bytes that are wrong with
arithmetic that is right. Every CRC on earth passes. Only regenerating the
expected payload from the seed detects it. This is why probes carry seeds
instead of relying on checksums, and why a probe that *loses* its seed header
reports `UNVERIFIED` rather than `OK`: usable, and honestly labeled.

**`EVICTED` is invisible without the map.** Context eviction and
summarization do not corrupt lines; they remove them, and nothing marks the
gap. The allocation map is the only thing that turns absence into a report.
The test suite proves the negative: strip `MAP` and `SECTORS` from a frame,
evict a sector, and the loss becomes silent — which is the property the map
exists to buy, and the property every unmapped context window has today.

**A vanished frame is invisible to itself.** A probe whose entire frame is
evicted takes its map down with it. So every probe's header lists the ids of
the probes emitted before it (`PREV`), and `emit --chain transcript.txt` does
this automatically. Any one surviving probe can then convict the medium of
having swallowed the others whole.

## Loss as a curve, not a number

Every report records where in the text each probe was found. Emit probes at
intervals as a conversation grows, scan at the end, and the per-probe results
are a survival curve over context depth — which is how you compare what
different harnesses, compaction strategies, and providers actually do to the
far end of the window. `scan --json` emits per-sector records with offsets
for exactly this.

## Wear simulation

Every corruption the scanner claims to detect ships with an injector that
produces it, because a persistence layer whose failure modes have never been
exercised fails silently in production:

```console
$ python3 -m ctxprobe rot transcript.txt --evict 0.25 --bitrot 2 --rewrite 1
$ python3 -m ctxprobe rot transcript.txt --truncate 0.5     # oldest half gone
$ python3 -m ctxprobe rot transcript.txt --strip-headers    # "summarization"
```

`rot` is also the rehearsal tool: before trusting state to a medium, wear a
copy down and check the scan says what you'd need it to say.

## Library use

```python
import ctxprobe

frame, seed, probe_id = ctxprobe.emit(sectors=16, label="before-compaction")
# ... place frame into the medium, let the world happen to it ...
report = ctxprobe.scan(surviving_text, expect=[probe_id])
print(report.report())
report.to_json()          # machine-readable, for charts
report.clean              # True only if every sector of every probe survived
```

## Relationship to TRAPCPU

TRAPCPU discovered the problem: its transcript-as-disk snapshots needed
per-sector CRCs and an allocation map because the attention window loses data
in a way no disk does. `ctxprobe` is the generalization — the same integrity
discipline with the machine removed, pointed at any text that anything
depends on. TRAPCPU's snapshot layer remains its own implementation; the two
share a CRC polynomial and a worldview, not code.
