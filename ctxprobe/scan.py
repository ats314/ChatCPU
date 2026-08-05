"""Scanning: mount whatever survived and say precisely what did not.

``scan(text)`` walks any blob of text — a saved transcript, a compacted
context, the output of a summarizer — finds every probe frame it can still
read, and classifies every sector the frames promise. The taxonomy matters
more than the totals: each status names a *different physical failure* of the
medium, and conflating them is how loss stays invisible.

======================  =====================================================
Status                  What actually happened to the bytes
======================  =====================================================
``OK``                  present, checksum verifies, and the payload matches
                        what the seed regenerates: survived byte-for-byte.
``UNVERIFIED``          checksum verifies but the frame lost its ``SEED``
                        header, so byte-for-byte survival cannot be proven.
                        Usable, and honestly labeled — the DEGRADED state.
``REWRITTEN``           checksum verifies but the payload is *wrong*. Someone
                        altered the bytes and recomputed the CRC. Only the
                        seed comparison can see this; a checksum never will.
``CORRUPTED``           the sector line is parseable but its CRC fails:
                        in-place mangling (reflow, truncation, typo repair).
``MANGLED``             the line no longer parses as a sector at all.
``EVICTED``             the allocation map lists a sector for which no line
                        exists. The line is not damaged — it is *gone*, which
                        is what context eviction and summarization look like.
======================  =====================================================

Whole-frame loss is the failure a frame cannot report about itself. It is
covered from the outside: every id referenced by a surviving probe's ``PREV``
chain (or passed in ``expect``) that no frame answers for is reported as a
vanished probe.

A sector line mangled beyond even an ``@index`` prefix cannot be attributed
to its slot, so it is counted as unattributed and its slot will *also* show
as EVICTED. The double count is deliberate: pretending to know which slot an
unreadable line belonged to would be restoring plausible garbage.
"""

import json
import re

from .frame import (
    DATA_CLOSE,
    DATA_OPEN,
    SECTOR_BYTES,
    crc16,
    sector_payload,
)

_BEGIN_RE = re.compile(r"^===\s*CTXPROBE/(\d+)\s+SENTINEL\s*===\s*$")
_END_RE = re.compile(r"^===\s*CTXPROBE\s+END\s*===\s*$")
_HEADER_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*:\s*(.*)$")
_SECTOR_RE = re.compile(r"^@([0-9A-Fa-f]{1,2})\s+([0-9A-Fa-f]+)\s+([0-9A-Fa-f]{4})\s*$")
_ID_RE = re.compile(r"^[0-9A-Fa-f]{1,16}$")


class Status:
    OK = "OK"
    UNVERIFIED = "UNVERIFIED"
    REWRITTEN = "REWRITTEN"
    CORRUPTED = "CORRUPTED"
    MANGLED = "MANGLED"
    EVICTED = "EVICTED"

    ALL = (OK, UNVERIFIED, REWRITTEN, CORRUPTED, MANGLED, EVICTED)

    #: One character per status, for loss maps.
    GLYPH = {
        OK: ".",
        UNVERIFIED: "?",
        REWRITTEN: "~",
        CORRUPTED: "x",
        MANGLED: "#",
        EVICTED: "_",
    }

    #: Statuses whose bytes did not survive.
    LOST = (REWRITTEN, CORRUPTED, MANGLED, EVICTED)


class SectorState:
    """The verdict on one sector."""

    __slots__ = ("index", "status", "detail", "stray")

    def __init__(self, index, status, detail="", stray=False):
        self.index = index
        self.status = status
        self.detail = detail
        self.stray = stray

    def __repr__(self):
        where = "?" if self.index is None else "%02X" % self.index
        return "<SectorState @%s %s>" % (where, self.status)


class _Frame:
    """One parsed probe frame, before classification."""

    def __init__(self):
        self.version = 0
        self.start_line = 0
        self.terminated = False
        self.probe_id = None
        self.seed = None
        self.label = None
        self.declared_sectors = None
        self.sector_bytes = SECTOR_BYTES
        self.prev = []
        self.map_indices = None
        self.metacrc = None
        self.meta_lines = []
        self.sectors = []       # (index, blob, crc_ok)
        self.unreadable = []    # (index_or_None, raw_line)


class ProbeReport:
    """What one probe says about the medium it was stored in."""

    def __init__(self, frame, states, notes):
        self.probe_id = frame.probe_id or "????????"
        self.seed = frame.seed
        self.label = frame.label
        self.start_line = frame.start_line
        self.prev = list(frame.prev)
        self.map_present = frame.map_indices is not None
        self.metacrc_ok = _metacrc_ok(frame)
        self.sector_bytes = frame.sector_bytes
        self.states = states
        self.notes = notes

    @property
    def counts(self):
        out = dict.fromkeys(Status.ALL, 0)
        for state in self.states:
            out[state.status] += 1
        return out

    @property
    def bytes_intact(self):
        good = (Status.OK, Status.UNVERIFIED)
        return sum(self.sector_bytes for s in self.states if s.status in good)

    @property
    def bytes_lost(self):
        return sum(self.sector_bytes for s in self.states
                   if s.status in Status.LOST)

    @property
    def clean(self):
        return (self.metacrc_ok
                and all(s.status == Status.OK for s in self.states))

    def lossmap(self):
        """One glyph per mapped sector, in index order: ``[..x.._~.]``."""
        slots = sorted(
            (s for s in self.states if s.index is not None and not s.stray),
            key=lambda s: s.index,
        )
        body = "".join(Status.GLYPH[s.status] for s in slots)
        extra = sum(1 for s in self.states if s.index is None or s.stray)
        return "[%s]%s" % (body, "+%d" % extra if extra else "")

    def summary(self):
        state = "CLEAN" if self.clean else "DEGRADED"
        line = "probe %s @line %d %s %s intact=%dB" % (
            self.probe_id, self.start_line + 1, state, self.lossmap(),
            self.bytes_intact,
        )
        if self.bytes_lost:
            line += " lost=%dB" % self.bytes_lost
        if not self.metacrc_ok:
            line += " metacrc=MISMATCH"
        return line

    def report(self):
        lines = [self.summary()]
        for state in self.states:
            if state.status == Status.OK:
                continue
            where = "@??" if state.index is None else "@%02X" % state.index
            lines.append("    sector %s %s%s" % (
                where, state.status,
                ": %s" % state.detail if state.detail else "",
            ))
        lines.extend("    note: %s" % note for note in self.notes)
        return "\n".join(lines)


class ScanReport:
    """Everything the transcript still admits to."""

    def __init__(self, probes, vanished, frames_seen, total_lines):
        self.probes = probes
        self.vanished = vanished  # {probe_id: reason}
        self.frames_seen = frames_seen
        self.total_lines = total_lines

    @property
    def totals(self):
        out = dict.fromkeys(Status.ALL, 0)
        for probe in self.probes:
            for status, count in probe.counts.items():
                out[status] += count
        return out

    @property
    def clean(self):
        return not self.vanished and all(p.clean for p in self.probes)

    def summary(self):
        totals = self.totals
        sectors = sum(totals.values())
        parts = ["%d %s" % (totals[status], status.lower())
                 for status in Status.ALL if totals[status]]
        line = "CTXPROBE SCAN: %d probe(s)" % len(self.probes)
        if self.vanished:
            line += ", %d vanished" % len(self.vanished)
        line += ", %d sector(s)" % sectors
        if parts:
            line += ": " + ", ".join(parts)
        return line

    def report(self):
        lines = [self.summary()]
        for probe in self.probes:
            depth = ""
            if self.total_lines:
                depth = " (depth %d%%)" % (
                    100 * probe.start_line // self.total_lines)
            body = probe.report().splitlines()
            lines.append("  " + body[0] + depth)
            lines.extend("  " + rest for rest in body[1:])
        for probe_id, reason in sorted(self.vanished.items()):
            lines.append("  vanished: probe %s (%s)" % (probe_id, reason))
        return "\n".join(lines)

    def to_json(self, indent=None):
        return json.dumps({
            "frames_seen": self.frames_seen,
            "total_lines": self.total_lines,
            "clean": self.clean,
            "totals": self.totals,
            "vanished": {pid: reason
                         for pid, reason in sorted(self.vanished.items())},
            "probes": [{
                "id": probe.probe_id,
                "label": probe.label,
                "offset_lines": probe.start_line,
                "metacrc_ok": probe.metacrc_ok,
                "map_present": probe.map_present,
                "sector_bytes": probe.sector_bytes,
                "clean": probe.clean,
                "bytes_intact": probe.bytes_intact,
                "bytes_lost": probe.bytes_lost,
                "lossmap": probe.lossmap(),
                "counts": probe.counts,
                "sectors": [{
                    "index": state.index,
                    "status": state.status,
                    "detail": state.detail,
                    "stray": state.stray,
                } for state in probe.states],
                "notes": probe.notes,
                "prev": probe.prev,
            } for probe in self.probes],
        }, indent=indent)


# ---------------------------------------------------------------------------
# PARSE
# ---------------------------------------------------------------------------

def find_probes(text):
    """Locate every probe frame in ``text``, in order of appearance."""
    lines = text.splitlines()
    frames = []
    index = 0

    while index < len(lines):
        match = _BEGIN_RE.match(lines[index].strip())
        if not match:
            index += 1
            continue

        frame = _Frame()
        frame.version = int(match.group(1))
        frame.start_line = index
        body = []
        index += 1
        while index < len(lines):
            stripped = lines[index].strip()
            if _END_RE.match(stripped):
                frame.terminated = True
                index += 1
                break
            if _BEGIN_RE.match(stripped):
                break
            body.append(lines[index])
            index += 1

        _parse_body(frame, body)
        frames.append(frame)

    return frames


def _parse_body(frame, body):
    in_data = False
    for raw in body:
        stripped = raw.strip()

        if stripped == DATA_OPEN:
            in_data = True
            continue
        if stripped == DATA_CLOSE:
            in_data = False
            continue

        if in_data:
            if stripped:
                _read_sector(frame, stripped)
            continue

        header = _HEADER_RE.match(stripped)
        if not header:
            if stripped:
                frame.meta_lines.append(stripped)
            continue

        key = header.group(1).upper()
        value = header.group(2).strip()
        if key == "METACRC":
            frame.metacrc = value
            continue

        frame.meta_lines.append(stripped)
        _read_header(frame, key, value)


def _read_header(frame, key, value):
    if key == "ID":
        if _ID_RE.match(value):
            frame.probe_id = value.upper()
    elif key == "SEED":
        try:
            frame.seed = int(value, 16) & ((1 << 64) - 1)
        except ValueError:
            frame.seed = None
    elif key == "LABEL":
        frame.label = value
    elif key == "SECTORS":
        frame.declared_sectors = int(value) if value.isdigit() else None
    elif key == "SECBYTES":
        if value.isdigit() and 1 <= int(value) <= 64:
            frame.sector_bytes = int(value)
    elif key == "PREV":
        frame.prev = [token.strip().upper() for token in value.split(",")
                      if _ID_RE.match(token.strip())]
    elif key == "MAP":
        indices = []
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                indices.append(int(token, 16))
            except ValueError:
                return  # a rotted map is no map
        frame.map_indices = indices


def _read_sector(frame, line):
    match = _SECTOR_RE.match(line)
    if not match:
        frame.unreadable.append((_leading_index(line), line))
        return

    index = int(match.group(1), 16)
    hex_body = match.group(2)
    declared = int(match.group(3), 16)

    if len(hex_body) % 2:
        frame.unreadable.append((index, line))
        return

    blob = bytes.fromhex(hex_body)
    frame.sectors.append((index, blob, crc16(blob) == declared))


def _leading_index(line):
    if line.startswith("@"):
        try:
            return int(line[1:3], 16)
        except ValueError:
            return None
    return None


def _metacrc_ok(frame):
    if frame.metacrc is None:
        return False
    try:
        declared = int(frame.metacrc, 16)
    except ValueError:
        return False
    return crc16("\n".join(frame.meta_lines).encode("utf-8")) == declared


# ---------------------------------------------------------------------------
# CLASSIFY
# ---------------------------------------------------------------------------

def _classify(frame):
    notes = []
    expected = frame.map_indices
    if expected is None:
        if frame.declared_sectors is not None:
            expected = list(range(frame.declared_sectors))
            notes.append("allocation map lost; expecting %d sectors from the "
                         "SECTORS header" % frame.declared_sectors)
        else:
            notes.append("allocation map and sector count both lost: "
                         "evicted sectors are SILENT in this report")

    if frame.seed is None:
        notes.append("seed lost; checksummed sectors are UNVERIFIED, and a "
                     "rewritten sector with a recomputed CRC is undetectable")

    states = []
    seen = set()
    expected_set = set(expected) if expected is not None else None

    for index, blob, crc_ok in frame.sectors:
        seen.add(index)
        stray = expected_set is not None and index not in expected_set
        if not crc_ok:
            states.append(SectorState(index, Status.CORRUPTED,
                                      "sector CRC mismatch", stray))
            continue
        if frame.seed is None:
            states.append(SectorState(index, Status.UNVERIFIED,
                                      "CRC ok, no seed to compare against",
                                      stray))
            continue
        wanted = sector_payload(frame.seed, index, frame.sector_bytes)
        if blob == wanted:
            states.append(SectorState(
                index, Status.OK,
                "not in allocation map" if stray else "", stray))
        else:
            states.append(SectorState(
                index, Status.REWRITTEN,
                "payload differs from seed regeneration but CRC verifies",
                stray))

    for index, raw in frame.unreadable:
        if index is not None:
            seen.add(index)
        states.append(SectorState(index, Status.MANGLED,
                                  "unreadable sector line"))

    if expected is not None:
        for index in expected:
            if index not in seen:
                states.append(SectorState(
                    index, Status.EVICTED,
                    "missing from transcript (evicted)"))

    return states, notes


# ---------------------------------------------------------------------------
# SCAN
# ---------------------------------------------------------------------------

def scan(text, expect=()):
    """Scan ``text`` for probes and produce a :class:`ScanReport`.

    ``expect`` is an optional iterable of probe ids the caller knows were
    emitted; ids in it that no surviving frame answers for are reported as
    vanished, exactly like ids referenced by a ``PREV`` chain.
    """
    frames = find_probes(text)
    probes = []
    seen_ids = set()
    referenced = {}  # id -> how many surviving probes vouch for it

    for frame in frames:
        states, notes = _classify(frame)
        if not frame.terminated:
            notes.append("frame never terminated; its tail was cut off")
        report = ProbeReport(frame, states, notes)
        probes.append(report)
        if frame.probe_id:
            seen_ids.add(frame.probe_id)
        for prev_id in frame.prev:
            referenced[prev_id] = referenced.get(prev_id, 0) + 1

    vanished = {}
    for probe_id, count in referenced.items():
        if probe_id not in seen_ids:
            vanished[probe_id] = (
                "referenced by %d surviving probe(s), no frame found" % count)
    for probe_id in expect:
        probe_id = probe_id.strip().upper()
        if probe_id and probe_id not in seen_ids:
            vanished.setdefault(probe_id, "expected by caller, no frame found")

    return ScanReport(
        probes=probes,
        vanished=vanished,
        frames_seen=len(frames),
        total_lines=len(text.splitlines()),
    )
