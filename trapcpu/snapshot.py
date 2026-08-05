"""Transcript as disk: serialize a machine into text, mount it back out.

ChatCPU persisted through the browser's Cache Storage. TRAPCPU persists through
the conversation. A snapshot is a text frame; you emit it into the chat, and on
the next boot you scan the transcript back for it. The storage medium is the
attention window, which gives the layer a failure mode no real disk has and a
very familiar one: the far end of the platter fades.

That is not a metaphor here, it is the implementation. Memory is written out as
independently checksummed sectors, so when part of a transcript is evicted,
summarized, or reflowed, :func:`mount` can say precisely which addresses were
lost instead of restoring plausible garbage. ``MountReport.bytes_lost`` is the
bit rot figure.

Layout::

    === TRAPCPU/1 SNAPSHOT ===
    GEN: 3
    ...header lines...
    RAM>>>
    @0300 4F5201...  9B14      <- address, 32 bytes of hex, sector CRC
    <<<RAM
    SNAPCRC: 77C3
    === TRAPCPU END ===
"""

import re

from .protocol import FRAME_END, PROTOCOL_VERSION, Status, crc16, status_name

SNAPSHOT_BEGIN = f"=== TRAPCPU/{PROTOCOL_VERSION} SNAPSHOT ==="
SECTOR_BYTES = 32

_BEGIN_RE = re.compile(r"^===\s*TRAPCPU/(\d+)\s+SNAPSHOT\s*===\s*$")
_END_RE = re.compile(r"^===\s*TRAPCPU\s+END\s*===\s*$")
_HEADER_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*:\s*(.*)$")
_SECTOR_RE = re.compile(r"^@([0-9A-Fa-f]{1,4})\s+([0-9A-Fa-f]+)\s+([0-9A-Fa-f]{4})\s*$")

ROM_OPEN, ROM_CLOSE = "ROM>>>", "<<<ROM"
RAM_OPEN, RAM_CLOSE = "RAM>>>", "<<<RAM"
PROMPT_OPEN, PROMPT_CLOSE = "PENDINGPROMPT>>>", "<<<PENDINGPROMPT"


class SnapshotError(Exception):
    pass


class BadSector:
    """One sector that did not survive the round trip."""

    __slots__ = ("space", "address", "length", "reason")

    def __init__(self, space, address, length, reason):
        self.space = space
        self.address = address
        self.length = length
        self.reason = reason

    def __repr__(self):
        return (
            f"<BadSector {self.space} {self.address:04X}+{self.length} "
            f"{self.reason}>"
        )


class MountReport:
    """What survived, and what did not."""

    def __init__(self, generation=0, name="", frames_seen=0, snapcrc_ok=True,
                 bad_sectors=None, notes=None, sectors=0, bytes_restored=0):
        self.generation = generation
        self.name = name
        self.frames_seen = frames_seen
        self.snapcrc_ok = snapcrc_ok
        self.bad_sectors = bad_sectors or []
        self.notes = notes or []
        self.sectors = sectors
        self.bytes_restored = bytes_restored

    @property
    def bytes_lost(self):
        return sum(sector.length for sector in self.bad_sectors)

    @property
    def clean(self):
        return self.snapcrc_ok and not self.bad_sectors

    def summary(self):
        state = "CLEAN" if self.clean else "DEGRADED"
        line = (
            f"MOUNT {state} gen={self.generation} "
            f"sectors={self.sectors} restored={self.bytes_restored}B"
        )
        if self.bad_sectors:
            line += f" lost={self.bytes_lost}B in {len(self.bad_sectors)} sector(s)"
        if not self.snapcrc_ok:
            line += " snapcrc=MISMATCH"
        return line

    def report(self):
        lines = [self.summary()]
        for sector in self.bad_sectors:
            lines.append(
                f"    bad sector {sector.space} @{sector.address:04X} "
                f"({sector.length}B): {sector.reason}"
            )
        lines.extend(f"    note: {note}" for note in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# WRITE
# ---------------------------------------------------------------------------

def _sectors(memory, chunk=SECTOR_BYTES):
    """Yield ``(address, bytes)`` for every non-zero aligned chunk."""
    for base in range(0, len(memory), chunk):
        window = memory[base:base + chunk]
        if any(window):
            yield base, bytes(window)


def _sector_line(address, blob):
    return f"@{address:04X} {blob.hex().upper()} {crc16(blob):04X}"


def dump(machine, generation=None, name=None, include_rom=True):
    """Serialize ``machine`` into a snapshot frame."""
    generation = machine.generation + 1 if generation is None else generation
    name = machine.name if name is None else name

    body = [
        f"GEN: {generation}",
        f"NAME: {name}",
        f"CYCLE: {machine.cycles}",
        f"STATE: {machine.state}",
        "REGS: " + " ".join([
            f"A={machine.A:04X}", f"B={machine.B:04X}",
            f"C={machine.C:04X}", f"D={machine.D:04X}",
            f"PC={machine.PC:04X}", f"SP={machine.SP:04X}",
            f"Z={machine.Z}", f"CF={machine.CF}", f"N={machine.N}",
        ]),
        "ORACLE: " + " ".join([
            f"traps={machine.stats.traps}",
            f"attempts={machine.stats.attempts}",
            f"ok={machine.stats.ok}",
            f"degraded={machine.stats.degraded}",
            f"failed={machine.stats.failed}",
            f"retried={machine.stats.retried}",
            f"corrected={machine.stats.corrected}",
            f"discarded={machine.stats.discarded}",
            f"used={machine.traps_used}",
            f"budget={machine.oracle_budget}",
        ]),
        f"LASTSTATUS: {status_name(machine.last_oracle_status)}",
    ]

    if machine.oracle_ptr is not None:
        body.append(f"ORACLEPTR: {machine.oracle_ptr:04X}")

    pending = machine.pending
    if pending is not None:
        body.append("PENDING: " + " ".join([
            f"nonce={pending.nonce:04X}",
            f"attempt={pending.attempt}",
            f"retries={pending.retries}",
            f"mode={pending.mode}",
            f"replicas={pending.replicas}",
            f"capacity={pending.capacity}",
            f"flags={pending.flags:04X}",
            f"desc={pending.descriptor:04X}",
            f"resp={pending.response:04X}",
        ]))
        if pending.history:
            body.append("HISTORY: " + ",".join(
                status_name(status) for status, _ in pending.history
            ))
        body.append(PROMPT_OPEN)
        body.append(pending.prompt)
        body.append(PROMPT_CLOSE)

    rom_sectors = list(_sectors(machine.rom)) if include_rom else []
    ram_sectors = list(_sectors(machine.ram))

    # The maps are the allocation table of this disk. Without them, a sector
    # evicted from the transcript is simply absent and nothing notices; with
    # them, mount can name the addresses that went missing.
    if include_rom:
        body.append("ROMMAP: " + ",".join(f"{a:04X}" for a, _ in rom_sectors))
    body.append("RAMMAP: " + ",".join(f"{a:04X}" for a, _ in ram_sectors))

    if include_rom:
        body.append(ROM_OPEN)
        body.extend(_sector_line(a, b) for a, b in rom_sectors)
        body.append(ROM_CLOSE)

    body.append(RAM_OPEN)
    body.extend(_sector_line(a, b) for a, b in ram_sectors)
    body.append(RAM_CLOSE)

    canonical = "\n".join(body)
    body.append(f"SNAPCRC: {crc16(canonical.encode('utf-8')):04X}")

    return "\n".join([SNAPSHOT_BEGIN] + body + [FRAME_END])


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------

class ParsedSnapshot:
    def __init__(self):
        self.version = PROTOCOL_VERSION
        self.generation = 0
        self.name = ""
        self.cycle = 0
        self.state = "READY"
        self.registers = {}
        self.oracle = {}
        self.last_status = Status.OK
        self.oracle_ptr = None
        self.pending = None
        self.history = []
        self.prompt = None
        self.rom = []
        self.ram = []
        self.maps = {}
        self.bad_sectors = []
        self.snapcrc = None
        self.snapcrc_ok = True
        self.terminated = False


def find_snapshots(text):
    """Locate every snapshot frame in a transcript, in order of appearance."""
    lines = text.splitlines()
    found = []
    index = 0

    while index < len(lines):
        match = _BEGIN_RE.match(lines[index])
        if not match:
            index += 1
            continue

        version = int(match.group(1))
        body = []
        terminated = False
        index += 1
        while index < len(lines):
            if _END_RE.match(lines[index]):
                terminated = True
                index += 1
                break
            if _BEGIN_RE.match(lines[index]):
                break
            body.append(lines[index])
            index += 1

        parsed = _parse_snapshot(version, body)
        parsed.terminated = terminated
        found.append(parsed)

    return found


def _parse_kv(text):
    out = {}
    for token in text.split():
        if "=" in token:
            key, _, value = token.partition("=")
            out[key] = value
    return out


def _parse_snapshot(version, body):
    snap = ParsedSnapshot()
    snap.version = version

    canonical = []
    section = None
    cursor = 0

    while cursor < len(body):
        raw = body[cursor]
        stripped = raw.strip()

        if stripped == PROMPT_OPEN:
            canonical.append(raw)
            collected = []
            cursor += 1
            while cursor < len(body) and body[cursor].strip() != PROMPT_CLOSE:
                collected.append(body[cursor])
                canonical.append(body[cursor])
                cursor += 1
            if cursor < len(body):
                canonical.append(body[cursor])
                cursor += 1
            snap.prompt = "\n".join(collected)
            continue

        if stripped in (ROM_OPEN, RAM_OPEN):
            section = "rom" if stripped == ROM_OPEN else "ram"
            canonical.append(raw)
            cursor += 1
            continue

        if stripped in (ROM_CLOSE, RAM_CLOSE):
            section = None
            canonical.append(raw)
            cursor += 1
            continue

        if section is not None:
            canonical.append(raw)
            _read_sector(snap, section, stripped)
            cursor += 1
            continue

        header = _HEADER_RE.match(stripped)
        if header:
            key = header.group(1).upper()
            value = header.group(2).strip()
            if key == "SNAPCRC":
                snap.snapcrc = value
                cursor += 1
                continue
            canonical.append(raw)
            _read_header(snap, key, value)
            cursor += 1
            continue

        canonical.append(raw)
        cursor += 1

    if snap.snapcrc is not None:
        expected = crc16("\n".join(canonical).encode("utf-8"))
        try:
            snap.snapcrc_ok = int(snap.snapcrc, 16) == expected
        except ValueError:
            snap.snapcrc_ok = False
    else:
        snap.snapcrc_ok = False

    return snap


def _read_header(snap, key, value):
    if key in ("ROMMAP", "RAMMAP"):
        space = "rom" if key == "ROMMAP" else "ram"
        addresses = []
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                addresses.append(int(token, 16))
            except ValueError:
                return  # a rotted map is no map; fall back to count-free mount
        snap.maps[space] = addresses
        return
    if key == "GEN":
        snap.generation = int(value) if value.isdigit() else 0
    elif key == "NAME":
        snap.name = value
    elif key == "CYCLE":
        snap.cycle = int(value) if value.isdigit() else 0
    elif key == "STATE":
        snap.state = value.upper()
    elif key == "REGS":
        snap.registers = _parse_kv(value)
    elif key == "ORACLE":
        snap.oracle = _parse_kv(value)
    elif key == "ORACLEPTR":
        try:
            snap.oracle_ptr = int(value, 16)
        except ValueError:
            snap.oracle_ptr = None
    elif key == "LASTSTATUS":
        for attribute, code in vars(Status).items():
            if attribute == value and isinstance(code, int):
                snap.last_status = code
    elif key == "PENDING":
        snap.pending = _parse_kv(value)
    elif key == "HISTORY":
        snap.history = [item for item in value.split(",") if item]


def _read_sector(snap, space, line):
    if not line:
        return

    match = _SECTOR_RE.match(line)
    if not match:
        address = _leading_address(line)
        snap.bad_sectors.append(
            BadSector(space, address, SECTOR_BYTES, "unreadable sector line")
        )
        return

    address = int(match.group(1), 16)
    hex_body = match.group(2)
    declared = int(match.group(3), 16)

    if len(hex_body) % 2:
        snap.bad_sectors.append(
            BadSector(space, address, (len(hex_body) + 1) // 2, "odd hex length")
        )
        return

    blob = bytes.fromhex(hex_body)
    if crc16(blob) != declared:
        snap.bad_sectors.append(
            BadSector(space, address, len(blob), "sector CRC mismatch")
        )
        return

    getattr(snap, space).append((address, blob))


def _leading_address(line):
    if line.startswith("@"):
        head = line[1:5]
        try:
            return int(head, 16)
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------------------
# MOUNT
# ---------------------------------------------------------------------------

def select(text):
    """Pick the snapshot to boot from: highest generation, latest on a tie.

    Returns ``(snapshot | None, frames_seen)``. A frame that lost its ``REGS``
    line to eviction is not a candidate: without registers there is no machine
    to restore, and guessing one would be worse than failing to mount.
    """
    frames = find_snapshots(text)
    candidates = [snap for snap in frames if snap.terminated and snap.registers]
    if not candidates:
        return None, len(frames)

    best = candidates[0]
    for snap in candidates[1:]:
        if snap.generation >= best.generation:
            best = snap
    return best, len(frames)


def _missing_sectors(snap):
    """Sectors the map promised that the transcript no longer contains."""
    missing = []
    for space in ("rom", "ram"):
        expected = snap.maps.get(space)
        if expected is None:
            continue
        present = {address for address, _ in getattr(snap, space)}
        present.update(
            sector.address for sector in snap.bad_sectors
            if sector.space == space
        )
        for address in expected:
            if address not in present:
                missing.append(BadSector(
                    space, address, SECTOR_BYTES,
                    "sector missing from transcript (evicted)",
                ))
    return missing


def mount(text, machine=None, seed=None):
    """Restore a machine from a transcript. Returns ``(machine, report)``."""
    from .machine import Machine, PendingTrap, State

    snap, seen = select(text)
    if snap is None:
        raise SnapshotError("no complete snapshot frame found in transcript")

    machine = machine or Machine(seed=seed)
    machine.reset()

    for address, blob in snap.rom:
        machine.rom[address:address + len(blob)] = blob
    for address, blob in snap.ram:
        machine.ram[address:address + len(blob)] = blob

    registers = snap.registers
    machine.A = _hex(registers.get("A"))
    machine.B = _hex(registers.get("B"))
    machine.C = _hex(registers.get("C"))
    machine.D = _hex(registers.get("D"))
    machine.PC = _hex(registers.get("PC"))
    machine.SP = _hex(registers.get("SP"), 0xFFFF)
    machine.Z = _dec(registers.get("Z"))
    machine.CF = _dec(registers.get("CF"))
    machine.N = _dec(registers.get("N"))

    machine.cycles = snap.cycle
    machine.name = snap.name
    machine.generation = snap.generation
    machine.last_oracle_status = snap.last_status
    machine.oracle_ptr = snap.oracle_ptr

    oracle = snap.oracle
    machine.stats.traps = _dec(oracle.get("traps"))
    machine.stats.attempts = _dec(oracle.get("attempts"))
    machine.stats.ok = _dec(oracle.get("ok"))
    machine.stats.degraded = _dec(oracle.get("degraded"))
    machine.stats.failed = _dec(oracle.get("failed"))
    machine.stats.retried = _dec(oracle.get("retried"))
    machine.stats.corrected = _dec(oracle.get("corrected"))
    machine.stats.discarded = _dec(oracle.get("discarded"))
    machine.traps_used = _dec(oracle.get("used"))
    if oracle.get("budget"):
        machine.oracle_budget = _dec(oracle.get("budget"))

    notes = []
    if snap.state == State.TRAPPED and snap.pending:
        pending = snap.pending
        machine.pending = PendingTrap(
            descriptor=_hex(pending.get("desc")),
            mode=_dec(pending.get("mode")),
            replicas=_dec(pending.get("replicas"), 1),
            capacity=_dec(pending.get("capacity"), 1),
            retries=_dec(pending.get("retries")),
            flags=_hex(pending.get("flags")),
            response=_hex(pending.get("resp")),
            prompt=snap.prompt or "",
            attempt=_dec(pending.get("attempt"), 1),
            nonce=_hex(pending.get("nonce")),
            history=[(Status.MALFORMED, name) for name in snap.history],
        )
        machine.state = State.TRAPPED
        notes.append(
            f"resumed mid-trap: nonce {machine.pending.nonce:04X} "
            f"attempt {machine.pending.attempt}"
        )
        if not snap.prompt:
            notes.append("pending prompt was lost; the retry will ask nothing")
    else:
        machine.state = snap.state if snap.state in vars(State).values() else State.READY
        if machine.state == State.TRAPPED:
            machine.state = State.FAULT
            notes.append("snapshot claimed TRAPPED but carried no pending request")

    bad = list(snap.bad_sectors) + _missing_sectors(snap)

    report = MountReport(
        generation=snap.generation,
        name=snap.name,
        frames_seen=seen,
        snapcrc_ok=snap.snapcrc_ok,
        bad_sectors=bad,
        notes=notes,
        sectors=len(snap.rom) + len(snap.ram) + len(bad),
        bytes_restored=sum(len(b) for _, b in snap.rom)
        + sum(len(b) for _, b in snap.ram),
    )

    if not snap.snapcrc_ok:
        report.notes.append(
            "snapshot header CRC did not verify; sectors were checked individually"
        )

    return machine, report


def _hex(text, default=0):
    if not text:
        return default
    try:
        return int(text, 16) & 0xFFFF
    except ValueError:
        return default


def _dec(text, default=0):
    if not text:
        return default
    try:
        return int(text, 10)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# BIT ROT SIMULATION
# ---------------------------------------------------------------------------

def simulate_eviction(text, fraction=0.25, rng=None):
    """Delete a fraction of the memory sector lines: context eviction.

    Nothing marks the gap. This is what makes the transcript a lossy medium and
    why every sector carries its own address rather than relying on order.
    """
    import random as _random
    rng = rng or _random.Random(0)

    lines = text.split("\n")
    sector_indexes = [
        index for index, line in enumerate(lines)
        if _SECTOR_RE.match(line.strip())
    ]
    drop_count = int(len(sector_indexes) * fraction)
    doomed = set(rng.sample(sector_indexes, drop_count)) if drop_count else set()

    return "\n".join(
        line for index, line in enumerate(lines) if index not in doomed
    )


def simulate_bitrot(text, flips=1, rng=None):
    """Mangle hex digits inside sector lines: the platter fades, CRC catches it."""
    import random as _random
    rng = rng or _random.Random(0)

    lines = text.split("\n")
    sector_indexes = [
        index for index, line in enumerate(lines)
        if _SECTOR_RE.match(line.strip())
    ]
    if not sector_indexes:
        return text

    for _ in range(flips):
        index = rng.choice(sector_indexes)
        match = _SECTOR_RE.match(lines[index].strip())
        address, body, checksum = match.groups()
        position = rng.randrange(len(body))
        digit = body[position]
        replacement = "0" if digit != "0" else "F"
        body = body[:position] + replacement + body[position + 1:]
        lines[index] = f"@{address} {body} {checksum}"

    return "\n".join(lines)
