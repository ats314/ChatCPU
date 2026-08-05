"""The TRAPCPU machine: a 16-bit CPU with a language model on the bus.

The interesting part of this file is not the interpreter, it is the suspend
point. ``TRAP`` does not call anything. It unwinds the run loop and hands the
host a :class:`~trapcpu.protocol.TrapFrame`, and the machine sits frozen until
somebody calls :meth:`Machine.resume` with text. Whether that text came from a
model, a human, or a file on disk is not the CPU's problem, which is exactly
what makes the model a peripheral rather than a library call.

Typical drive loop::

    machine = Machine()
    machine.load(assemble(source))
    result = machine.run()
    while result.state is State.TRAPPED:
        result = machine.resume(oracle.ask(result.frame))
"""

import random
from collections import Counter

from .assembler import Program
from .isa import (
    OPS,
    PORT_IVEC,
    PORT_KEY,
    PORT_KEY_STATE,
    PORT_MPROT,
    PORT_ORACLE,
    PORT_ORACLE_END,
    PORT_ORACLE_STAT,
    PORT_RANDOM,
    PORT_SCREEN,
)
from .protocol import (
    DESCRIPTOR_MAGIC,
    DESCRIPTOR_SIZE,
    FIELD_ATTEMPTS,
    FIELD_CAPACITY,
    FIELD_CORRECTED,
    FIELD_FLAGS,
    FIELD_LENGTH,
    FIELD_MAGIC,
    FIELD_MODE,
    FIELD_NONCE,
    FIELD_PROMPT,
    FIELD_REPLICAS,
    FIELD_RESPONSE,
    FIELD_RETRIES,
    FIELD_STATUS,
    FIELD_VALUE,
    FIELD_VERSION,
    MAX_PROMPT,
    MAX_REPLICAS,
    MAX_RETRIES,
    MODE_NAMES,
    PROTOCOL_VERSION,
    Flag,
    Mode,
    OracleResult,
    Status,
    TrapFrame,
    is_success,
    status_name,
    validate,
)

RAM_SIZE = 64 * 1024
ROM_SIZE = 64 * 1024

SCREEN_W = 32
SCREEN_H = 16

DEFAULT_CYCLE_LIMIT = 1_000_000
DEFAULT_ORACLE_BUDGET = 64
MAX_STRING = 4096


class State:
    READY = "READY"
    RUNNING = "RUNNING"
    TRAPPED = "TRAPPED"
    ASYNC = "ASYNC"
    HALTED = "HALTED"
    LIMIT = "LIMIT"
    FAULT = "FAULT"


class CPUFault(Exception):
    """A fault the guest program caused: bad opcode, bad access."""


class _Suspend(Exception):
    """Internal control flow. Unwinds the run loop at a trap."""


class _AsyncPublish(Exception):
    """Internal control flow. Surfaces an async frame; the machine stays
    runnable — the whole point of TRAPA is that execution continues."""


class MachineError(Exception):
    """A host level misuse of the machine API."""


# ---------------------------------------------------------------------------
# RESULTS AND BOOKKEEPING
# ---------------------------------------------------------------------------

class RunResult:
    """What came back from :meth:`Machine.run` or :meth:`Machine.resume`."""

    __slots__ = ("state", "frame", "reason", "cycles", "output")

    def __init__(self, state, frame=None, reason="", cycles=0, output=""):
        self.state = state
        self.frame = frame
        self.reason = reason
        self.cycles = cycles
        self.output = output

    @property
    def trapped(self):
        return self.state == State.TRAPPED

    @property
    def halted(self):
        return self.state == State.HALTED

    def __repr__(self):
        detail = f" reason={self.reason!r}" if self.reason else ""
        return f"<RunResult {self.state} cycles={self.cycles}{detail}>"


class OracleStats:
    """Counters for the stochastic component. The whole point is measurability."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.traps = 0
        self.attempts = 0
        self.ok = 0
        self.degraded = 0
        self.failed = 0
        self.retried = 0
        self.refusals = 0
        self.corrected = 0
        self.discarded = 0
        self.unchecked = 0
        self.by_status = Counter()

    def record_attempt(self, status):
        self.attempts += 1
        self.by_status[status] += 1
        if status == Status.REFUSED:
            self.refusals += 1

    def summary(self):
        parts = [
            f"traps={self.traps}",
            f"attempts={self.attempts}",
            f"ok={self.ok}",
            f"degraded={self.degraded}",
            f"failed={self.failed}",
            f"retried={self.retried}",
            f"corrected={self.corrected}",
        ]
        return " ".join(parts)

    def report(self):
        lines = [
            f"traps issued      : {self.traps}",
            f"attempts spent    : {self.attempts}",
            f"completed OK      : {self.ok}",
            f"completed DEGRADED: {self.degraded}",
            f"completed FAILED  : {self.failed}",
            f"retries consumed  : {self.retried}",
            f"bytes repaired    : {self.corrected}",
            f"replicas discarded: {self.discarded}",
        ]
        if self.by_status:
            lines.append("attempt outcomes  :")
            for status, count in sorted(self.by_status.items()):
                lines.append(f"    {status_name(status):<16} {count}")
        return "\n".join(lines)


class PendingTrap:
    """An oracle request the machine is currently blocked on."""

    __slots__ = (
        "descriptor", "mode", "replicas", "capacity", "retries", "flags",
        "response", "prompt", "attempt", "nonce", "history", "channel",
        "device",
    )

    def __init__(self, descriptor, mode, replicas, capacity, retries, flags,
                 response, prompt, attempt=1, nonce=0, history=None,
                 channel="SYNC", device=0):
        self.descriptor = descriptor
        self.mode = mode
        self.replicas = replicas
        self.capacity = capacity
        self.retries = retries
        self.flags = flags
        self.response = response
        self.prompt = prompt
        self.attempt = attempt
        self.nonce = nonce
        self.history = history if history is not None else []
        self.channel = channel
        self.device = device

    def to_frame(self, machine):
        last_status = self.history[-1][0] if self.history else None
        last_detail = self.history[-1][1] if self.history else None
        return TrapFrame(
            nonce=self.nonce,
            prompt=self.prompt,
            mode=self.mode,
            replicas=self.replicas,
            capacity=self.capacity,
            attempt=self.attempt,
            retries=self.retries,
            flags=self.flags,
            registers=machine.register_map(),
            cycle=machine.cycles,
            descriptor=self.descriptor,
            last_status=last_status,
            last_detail=last_detail,
            channel=self.channel,
            device=self.device,
        )


# ---------------------------------------------------------------------------
# PERIPHERALS
# ---------------------------------------------------------------------------

class Hardware:
    """Screen, keyboard queue and entropy. Unchanged from ChatCPU in spirit."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.machine = None
        self.reset()

    def reset(self):
        self.keys = []
        self.clear_screen()

    def attach(self, machine):
        self.machine = machine

    def key(self, value):
        if not value:
            return
        self.keys.append(ord(str(value)[0]) & 0xFF)

    def keycode(self, value):
        self.keys.append(int(value) & 0xFF)

    def clear_screen(self):
        self.screen = [[" "] * SCREEN_W for _ in range(SCREEN_H)]

    def render(self):
        return "\n".join("".join(row) for row in self.screen)

    def read_port(self, port):
        port &= 0xFF
        if port == PORT_KEY:
            return self.keys.pop(0) if self.keys else 0
        if port == PORT_KEY_STATE:
            return int(bool(self.keys))
        if port == PORT_RANDOM:
            return self.rng.randrange(0, 256)
        if port == PORT_ORACLE_STAT:
            return self.machine.last_oracle_status if self.machine else 0
        if port == PORT_IVEC:
            machine = self.machine
            if machine is None:
                return 0
            if machine.async_pending is not None:
                return 1
            if machine.irq_pending:
                return 2
            return 0
        return 0

    def write_port(self, port, value):
        port &= 0xFF
        machine = self.machine
        if machine is None:
            return
        if port == PORT_SCREEN:
            x, y = machine.B, machine.C
            if 0 <= x < SCREEN_W and 0 <= y < SCREEN_H:
                self.screen[y][x] = chr(machine.A & 0xFF)
        elif PORT_ORACLE <= port <= PORT_ORACLE_END:
            machine.oracle_ptr = value & 0xFFFF
            machine.oracle_device = port - PORT_ORACLE
        elif port == PORT_IVEC:
            machine.ivec = value & 0xFFFF
        elif port == PORT_MPROT:
            machine.exec_map[(value >> 8) & 0xFF] = value & 1


# ---------------------------------------------------------------------------
# MACHINE
# ---------------------------------------------------------------------------

class Machine:

    def __init__(self, seed=None, oracle_budget=DEFAULT_ORACLE_BUDGET,
                 cycle_limit=DEFAULT_CYCLE_LIMIT):
        self.rng = random.Random(seed)
        self.hardware = Hardware(rng=self.rng)
        self.hardware.attach(self)
        self.oracle_budget = oracle_budget
        self.cycle_limit = cycle_limit
        self.stats = OracleStats()
        self.symbols = {}
        self.name = ""
        self.generation = 0
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self):
        self.ram = bytearray(RAM_SIZE)
        self.rom = bytearray(ROM_SIZE)

        self.A = self.B = self.C = self.D = 0
        self.PC = 0
        self.SP = 0xFFFF
        self.Z = self.CF = self.N = 0

        self.cycles = 0
        self.state = State.READY
        self.fault = ""
        self.output_buffer = []

        self.oracle_ptr = None
        self.oracle_device = 0
        self.pending = None
        self.async_pending = None
        self.last_oracle_status = Status.OK
        self.last_oracle_detail = ""
        self.last_oracle_notes = []
        self.traps_used = 0
        self.segment_limit = self.cycle_limit

        self.cs = 0                       # 0: fetch from ROM, 1: from RAM
        self.ivec = 0                     # interrupt vector
        self.ien = 0                      # interrupts masked at reset
        self.irq_pending = False
        self.exec_map = bytearray(256)    # per 256-byte-page execute permission

        self.hardware.reset()
        self.stats.reset()

    def load(self, program, reset=True):
        """Install a :class:`Program` (or raw ROM bytes) and preload RAM data."""
        if reset:
            self.reset()

        if isinstance(program, Program):
            code = program.code
            self.symbols = dict(program.symbols)
            segments = program.data
        else:
            code = bytes(program)
            segments = ()

        if len(code) > ROM_SIZE:
            raise MachineError(f"program too large: {len(code)} > {ROM_SIZE}")
        self.rom[:len(code)] = code

        for address, blob in segments:
            if address + len(blob) > RAM_SIZE:
                raise MachineError(
                    f"data segment at {address:04X} overruns RAM"
                )
            self.ram[address:address + len(blob)] = blob

        self.state = State.READY
        return self

    # -- memory ------------------------------------------------------------

    def read8(self, address):
        return self.ram[address & 0xFFFF]

    def write8(self, address, value):
        address &= 0xFFFF
        if self.exec_map[address >> 8]:
            raise CPUFault(
                f"W^X fault: write to executable page {address >> 8:02X} "
                f"(address {address:04X})"
            )
        self.ram[address] = value & 0xFF

    def read16(self, address):
        return self.read8(address) | (self.read8(address + 1) << 8)

    def write16(self, address, value):
        self.write8(address, value)
        self.write8(address + 1, value >> 8)

    def read_string(self, address, limit=MAX_STRING):
        out = bytearray()
        cursor = address & 0xFFFF
        for _ in range(limit):
            byte = self.ram[cursor]
            if byte == 0:
                return bytes(out), True
            out.append(byte)
            cursor = (cursor + 1) & 0xFFFF
        return bytes(out), False

    def push16(self, value):
        value &= 0xFFFF
        self.write8(self.SP, value)            # W^X applies to the stack too
        self.SP = (self.SP - 1) & 0xFFFF
        self.write8(self.SP, value >> 8)
        self.SP = (self.SP - 1) & 0xFFFF

    def pop16(self):
        self.SP = (self.SP + 1) & 0xFFFF
        hi = self.ram[self.SP]
        self.SP = (self.SP + 1) & 0xFFFF
        lo = self.ram[self.SP]
        return lo | (hi << 8)

    # -- flags -------------------------------------------------------------

    def flags(self, value):
        value &= 0xFFFF
        self.Z = int(value == 0)
        self.N = int(bool(value & 0x8000))

    def register_map(self):
        return {
            "A": self.A, "B": self.B, "C": self.C, "D": self.D,
            "PC": self.PC, "SP": self.SP,
            "Z": self.Z, "CF": self.CF, "N": self.N,
        }

    # -- fetch -------------------------------------------------------------

    def fetch8(self):
        if self.cs:
            if not self.exec_map[self.PC >> 8]:
                raise CPUFault(
                    f"NX fault: fetch from non-executable RAM page "
                    f"{self.PC >> 8:02X} (PC {self.PC:04X})"
                )
            value = self.ram[self.PC]
        else:
            value = self.rom[self.PC]
        self.PC = (self.PC + 1) & 0xFFFF
        return value

    def fetch16(self):
        lo = self.fetch8()
        hi = self.fetch8()
        return lo | (hi << 8)

    # -- execution ---------------------------------------------------------

    def step(self):
        op = self.fetch8()
        self.cycles += 1
        handler = _DISPATCH.get(op)
        if handler is None:
            raise CPUFault(f"invalid opcode {op:02X} at {(self.PC - 1) & 0xFFFF:04X}")
        handler(self)

    def run(self, limit=None):
        """Execute until halt, trap, fault, or the per-segment cycle budget.

        The budget applies to this segment of execution, not to the machine's
        lifetime, so a program that traps twenty times gets twenty budgets. It
        is remembered for the duration of the run so that resuming from a trap
        continues under the same limit instead of silently reverting to the
        default; calling ``run()`` again with no limit resets it.
        """
        if self.state in (State.HALTED, State.FAULT):
            return RunResult(self.state, reason=self.fault, cycles=0,
                             output=self.output())
        if self.state == State.TRAPPED:
            raise MachineError("machine is trapped; call resume() with a reply")

        budget = self.cycle_limit if limit is None else limit
        self.segment_limit = budget
        start = self.cycles
        self.state = State.RUNNING

        while True:
            if self.cycles - start >= budget:
                self.state = State.LIMIT
                return RunResult(State.LIMIT, reason=f"cycle budget {budget} spent",
                                 cycles=self.cycles - start, output=self.output())
            try:
                if self.irq_pending and self.ien:
                    self._deliver_irq()
                self.step()
            except _Suspend:
                return RunResult(State.TRAPPED, frame=self.pending.to_frame(self),
                                 cycles=self.cycles - start, output=self.output())
            except _AsyncPublish:
                # The machine is still runnable; only the run loop unwinds so
                # the host can see the frame. state stays RUNNING on purpose.
                return RunResult(State.ASYNC,
                                 frame=self.async_pending.to_frame(self),
                                 cycles=self.cycles - start, output=self.output())
            except CPUFault as error:
                self.state = State.FAULT
                self.fault = str(error)
                return RunResult(State.FAULT, reason=self.fault,
                                 cycles=self.cycles - start, output=self.output())

            if self.state == State.HALTED:
                return RunResult(State.HALTED, cycles=self.cycles - start,
                                 output=self.output())

    def output(self):
        return "".join(self.output_buffer)

    # -- oracle ------------------------------------------------------------

    def _next_nonce(self):
        source = self.async_pending or self.pending
        previous = source.nonce if source else None
        for _ in range(16):
            nonce = self.rng.randrange(1, 0x10000)
            if nonce != previous:
                return nonce
        return (previous or 0) ^ 0x5A5A or 1

    def _read_descriptor(self):
        """Validate the latched descriptor.

        Returns ``(PendingTrap | None, error, writable)``. ``writable`` is the
        descriptor address only once magic and version have checked out: a
        status writeback through a pointer that failed those is a wild store
        into guest memory, so a rejected descriptor gets its status in A alone.
        """
        pointer = self.oracle_ptr
        if pointer is None:
            return None, "no descriptor latched on port 0x30", None
        if pointer + DESCRIPTOR_SIZE > RAM_SIZE:
            return None, f"descriptor at {pointer:04X} overruns RAM", None

        magic = self.read16(pointer + FIELD_MAGIC)
        if magic != DESCRIPTOR_MAGIC:
            return None, f"descriptor magic {magic:04X} != {DESCRIPTOR_MAGIC:04X}", None

        version = self.read8(pointer + FIELD_VERSION)
        if version != PROTOCOL_VERSION:
            return None, f"descriptor version {version} != {PROTOCOL_VERSION}", None

        def reject(reason):
            return None, reason, pointer

        mode = self.read8(pointer + FIELD_MODE)
        if mode not in MODE_NAMES:
            return reject(f"unknown mode {mode}")

        replicas = self.read8(pointer + FIELD_REPLICAS) or 1
        if replicas > MAX_REPLICAS:
            return reject(f"replicas {replicas} exceeds {MAX_REPLICAS}")

        retries = self.read8(pointer + FIELD_RETRIES)
        if retries > MAX_RETRIES:
            return reject(f"retries {retries} exceeds {MAX_RETRIES}")

        capacity = self.read16(pointer + FIELD_CAPACITY)
        if capacity == 0:
            return reject("response capacity is zero")

        response = self.read16(pointer + FIELD_RESPONSE)
        if response + capacity > RAM_SIZE:
            return reject(f"response buffer at {response:04X} overruns RAM")

        prompt_ptr = self.read16(pointer + FIELD_PROMPT)
        raw, terminated = self.read_string(prompt_ptr)
        if not terminated:
            return reject(
                f"prompt at {prompt_ptr:04X} is not NUL terminated "
                f"within {MAX_PROMPT}B"
            )
        if not raw:
            return reject(f"prompt at {prompt_ptr:04X} is empty")

        flags = self.read16(pointer + FIELD_FLAGS)

        return PendingTrap(
            descriptor=pointer,
            mode=mode,
            replicas=replicas,
            capacity=capacity,
            retries=retries,
            flags=flags,
            response=response,
            prompt=raw.decode("utf-8", "replace"),
        ), None, pointer

    def _begin_trap(self):
        """Executed by the TRAP opcode. Suspends, or completes with an error.

        The descriptor is validated before the budget is consulted, so that a
        BUDGET completion has a trustworthy address to write its status into.
        Otherwise A and the descriptor would disagree, which is precisely the
        kind of quiet inconsistency this machine exists to make impossible.
        """
        if self.async_pending is not None or self.irq_pending:
            # Single-channel device: a synchronous trap cannot overtake an
            # asynchronous one that is still in flight or awaiting delivery.
            self._complete(Status.BUSY, detail="async channel busy")
            return

        pending, error, writable = self._read_descriptor()
        if pending is None:
            status = (
                Status.NO_REQUEST if self.oracle_ptr is None
                else Status.BAD_DESCRIPTOR
            )
            self._complete(status, detail=error, descriptor=writable)
            return

        if self.traps_used >= self.oracle_budget:
            self._complete(
                Status.BUDGET,
                detail="oracle budget exhausted",
                descriptor=pending.descriptor,
            )
            return

        pending.device = self.oracle_device

        self.pending = pending
        pending.nonce = self._next_nonce()
        self.traps_used += 1
        self.stats.traps += 1
        self.state = State.TRAPPED
        raise _Suspend()

    def _begin_async(self):
        """Executed by TRAPA. Publishes the frame and keeps the CPU running.

        Completion arrives later, as an interrupt: the reply is validated and
        written back by :meth:`resume`, the IRQ line goes high, and the next
        instruction boundary with interrupts enabled vectors through IVEC.
        Registers are NOT clobbered by an async completion — the handler reads
        the descriptor, which is what the descriptor is for.
        """
        if self.async_pending is not None or self.irq_pending:
            self._complete(Status.BUSY, detail="async channel busy")
            return

        pending, error, writable = self._read_descriptor()
        if pending is None:
            status = (
                Status.NO_REQUEST if self.oracle_ptr is None
                else Status.BAD_DESCRIPTOR
            )
            self._complete(status, detail=error, descriptor=writable)
            return

        if self.traps_used >= self.oracle_budget:
            self._complete(Status.BUDGET, detail="oracle budget exhausted",
                           descriptor=pending.descriptor)
            return

        pending.channel = "ASYNC"
        pending.device = self.oracle_device
        pending.nonce = self._next_nonce()
        self.async_pending = pending
        self.traps_used += 1
        self.stats.traps += 1
        raise _AsyncPublish()

    def _deliver_irq(self):
        """Vector through IVEC: push CS then PC, mask, jump. IRET undoes it."""
        self.push16(self.cs)
        self.push16(self.PC)
        self.cs = 0                      # handlers always run from ROM
        self.PC = self.ivec
        self.ien = 0                     # auto-mask until IRET
        self.irq_pending = False

    def resume(self, reply_text):
        """Feed the oracle's answer back in and continue, or retry.

        Serves both channels: a machine suspended on a synchronous TRAP (or
        parked at WFI), and a machine still running with an async request in
        flight. Async completions write the descriptor and raise the IRQ line
        instead of clobbering registers.
        """
        if self.async_pending is not None:
            pending = self.async_pending
        elif self.state == State.TRAPPED and self.pending is not None:
            pending = self.pending
        else:
            raise MachineError("machine is not waiting on a trap")

        return self._resume_pending(pending, reply_text)

    def _resume_pending(self, pending, reply_text):
        frame = pending.to_frame(self)
        result = validate(reply_text, frame)
        self.stats.record_attempt(result.status)

        if not result.ok and pending.attempt <= pending.retries:
            pending.history.append((result.status, result.detail))
            pending.attempt += 1
            pending.nonce = self._next_nonce()
            self.stats.retried += 1
            state = (State.TRAPPED if self.state == State.TRAPPED
                     else State.ASYNC)
            return RunResult(state, frame=pending.to_frame(self),
                             cycles=0, output=self.output())

        if pending.channel == "ASYNC":
            return self._finish_async(pending, result)
        return self._finish_trap(result)

    def fail_trap(self, status=Status.ABORT, detail=""):
        """Terminate the pending trap from the host side, without a reply."""
        if self.async_pending is not None:
            pending = self.async_pending
        elif self.state == State.TRAPPED and self.pending is not None:
            pending = self.pending
        else:
            raise MachineError("machine is not waiting on a trap")
        pending.history.append((status, detail))
        self.stats.record_attempt(status)
        if pending.channel == "ASYNC":
            return self._finish_async(pending, OracleResult(status, detail=detail))
        return self._finish_trap(OracleResult(status, detail=detail))

    def _wx_blocked(self, pending, result):
        """The IOMMU rule: the oracle's DMA may never land on executable
        pages. Checked at completion time, because the guest could have
        blessed the buffer's page between issuing the trap and the reply
        arriving — the write is what must be legal, not the request."""
        if not (result.ok and result.payload):
            return False
        start = pending.response
        end = start + min(len(result.payload), pending.capacity)
        first, last = start >> 8, max(start, end - 1) >> 8
        return any(self.exec_map[page] for page in range(first, last + 1))

    def _finish_trap(self, result):
        """Write the oracle's answer into RAM and let the program continue."""
        pending = self.pending

        if self._wx_blocked(pending, result):
            result = OracleResult(
                Status.WX,
                detail=f"response buffer {pending.response:04X} overlaps an "
                       f"executable page; writeback denied",
            )

        length = 0
        value = result.value

        if result.ok and result.payload:
            payload = result.payload
            room = pending.capacity
            blob = payload[:room]
            self.ram[pending.response:pending.response + len(blob)] = blob
            length = len(blob)

            terminate = (
                pending.mode == Mode.TEXT
                and not pending.flags & Flag.NO_NUL
                and length < room
            )
            if terminate:
                self.ram[pending.response + length] = 0

        self.stats.corrected += result.corrected
        self.stats.discarded += result.discarded
        if result.status == Status.OK:
            self.stats.ok += 1
        elif result.status == Status.DEGRADED:
            self.stats.degraded += 1
        else:
            self.stats.failed += 1

        self._writeback(
            pending.descriptor, result.status, length, pending.nonce,
            pending.attempt, result.corrected, value,
        )

        self.A = result.status
        self.B = length
        self.C = pending.attempt
        if value is not None:
            self.D = value
        self.flags(self.A)
        self.CF = 0 if is_success(result.status) else 1
        self.last_oracle_status = result.status
        self.last_oracle_detail = result.detail
        self.last_oracle_notes = list(result.notes)

        self.pending = None
        self.state = State.RUNNING
        return self.run(self.segment_limit)

    def _finish_async(self, pending, result):
        """Complete an async trap: descriptor writeback + IRQ, registers
        untouched. If the machine is parked at WFI it resumes; if it was
        running free the host simply continues it."""
        if self._wx_blocked(pending, result):
            result = OracleResult(
                Status.WX,
                detail=f"response buffer {pending.response:04X} overlaps an "
                       f"executable page; writeback denied",
            )

        length = 0
        if result.ok and result.payload:
            payload = result.payload[:pending.capacity]
            self.ram[pending.response:pending.response + len(payload)] = payload
            length = len(payload)
            if (pending.mode == Mode.TEXT
                    and not pending.flags & Flag.NO_NUL
                    and length < pending.capacity):
                self.ram[pending.response + length] = 0

        self.stats.corrected += result.corrected
        self.stats.discarded += result.discarded
        if result.status == Status.OK:
            self.stats.ok += 1
        elif result.status == Status.DEGRADED:
            self.stats.degraded += 1
        else:
            self.stats.failed += 1

        self._writeback(
            pending.descriptor, result.status, length, pending.nonce,
            pending.attempt, result.corrected, result.value,
        )

        self.last_oracle_status = result.status
        self.last_oracle_detail = result.detail
        self.last_oracle_notes = list(result.notes)

        self.async_pending = None
        self.irq_pending = True

        if self.state == State.TRAPPED and self.pending is pending:
            # The guest was parked at WFI waiting for exactly this.
            self.pending = None
            self.state = State.RUNNING
        return self.run(self.segment_limit)

    def _complete(self, status, detail="", descriptor=None, length=0):
        """Complete a trap inline, without ever suspending."""
        self.stats.traps += 1
        self.stats.record_attempt(status)
        self.stats.failed += 1
        if descriptor is not None and descriptor + DESCRIPTOR_SIZE <= RAM_SIZE:
            self._writeback(descriptor, status, length, 0, 0, 0, None)
        self.A = status
        self.B = length
        self.C = 0
        self.flags(self.A)
        self.CF = 0 if is_success(status) else 1
        self.last_oracle_status = status
        self.last_oracle_detail = detail

    def _writeback(self, descriptor, status, length, nonce, attempts,
                   corrected, value):
        if descriptor is None or descriptor + DESCRIPTOR_SIZE > RAM_SIZE:
            return
        self.write16(descriptor + FIELD_STATUS, status)
        self.write16(descriptor + FIELD_LENGTH, length)
        self.write16(descriptor + FIELD_NONCE, nonce)
        self.write8(descriptor + FIELD_ATTEMPTS, min(attempts, 0xFF))
        self.write8(descriptor + FIELD_CORRECTED, min(corrected, 0xFF))
        if value is not None:
            self.write16(descriptor + FIELD_VALUE, value)

    # -- convenience -------------------------------------------------------

    def execute(self, oracle, limit=None, max_traps=None, async_latency=0):
        """Run to completion, letting ``oracle`` answer every trap.

        ``async_latency`` simulates a slow device on the async channel: after
        an async frame is published, the machine runs that many further
        cycles before the reply is delivered — which is the entire point of
        TRAPA, so the default of 0 (instant device) undersells it.
        """
        result = self.run(limit)
        served = 0
        while result.state in (State.TRAPPED, State.ASYNC):
            if max_traps is not None and served >= max_traps:
                return self.fail_trap(Status.BUDGET, "host trap limit reached")
            reply = oracle.ask(result.frame)
            served += 1

            if result.state == State.ASYNC and async_latency:
                # The device is thinking; the machine keeps running.
                interim = self.run(async_latency)
                if interim.state in (State.HALTED, State.FAULT):
                    return interim  # the guest finished without the answer

            if reply is None:
                result = self.fail_trap(Status.RETRIES, "oracle produced no reply")
            else:
                result = self.resume(reply)
        return result

    def describe(self):
        lines = [
            f"A  = {self.A:04X} ({self.A})",
            f"B  = {self.B:04X} ({self.B})",
            f"C  = {self.C:04X} ({self.C})",
            f"D  = {self.D:04X} ({self.D})",
            f"PC = {self.PC:04X}",
            f"SP = {self.SP:04X}",
            f"Z  = {self.Z}   CF = {self.CF}   N = {self.N}",
            f"STATE  = {self.state}",
            f"CYCLES = {self.cycles}",
            f"ORACLE = {status_name(self.last_oracle_status)} "
            f"({self.traps_used}/{self.oracle_budget} traps used)",
            f"CS = {'RAM' if self.cs else 'ROM'}   IVEC = {self.ivec:04X}   "
            f"IEN = {self.ien}   IRQ = {int(self.irq_pending)}   "
            f"ASYNC = {'in-flight' if self.async_pending else 'idle'}",
        ]
        if self.last_oracle_detail:
            lines.append(f"    detail: {self.last_oracle_detail}")
        lines.extend(f"    note:   {note}" for note in self.last_oracle_notes)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# INSTRUCTION IMPLEMENTATIONS
# ---------------------------------------------------------------------------

def _op_nop(m):
    pass


def _op_ldia(m):
    m.A = m.fetch16()
    m.flags(m.A)


def _op_ldib(m):
    m.B = m.fetch16()


def _op_ldic(m):
    m.C = m.fetch16()


def _op_ldid(m):
    m.D = m.fetch16()


def _arith(m, value):
    m.CF = int(value > 0xFFFF or value < 0)
    m.A = value & 0xFFFF
    m.flags(m.A)


def _op_add(m):
    _arith(m, m.A + m.B)


def _op_sub(m):
    _arith(m, m.A - m.B)


def _op_inc(m):
    m.A = (m.A + 1) & 0xFFFF
    m.flags(m.A)


def _op_dec(m):
    m.A = (m.A - 1) & 0xFFFF
    m.flags(m.A)


def _op_sta(m):
    m.write16(m.fetch16(), m.A)


def _op_lda(m):
    m.A = m.read16(m.fetch16())
    m.flags(m.A)


def _op_jmp(m):
    m.PC = m.fetch16()


def _op_jz(m):
    address = m.fetch16()
    if m.Z:
        m.PC = address


def _op_jnz(m):
    address = m.fetch16()
    if not m.Z:
        m.PC = address


def _op_cmp(m):
    difference = m.A - m.B
    m.CF = int(difference < 0)
    m.flags(difference)


def _op_cmpc(m):
    difference = m.A - m.C
    m.CF = int(difference < 0)
    m.flags(difference)


def _op_push(m):
    m.push16(m.A)


def _op_pop(m):
    m.A = m.pop16()
    m.flags(m.A)


def _op_call(m):
    address = m.fetch16()
    m.push16(m.PC)
    m.PC = address


def _op_ret(m):
    m.PC = m.pop16()


def _op_out(m):
    m.output_buffer.append(chr(m.A & 0xFF))


def _op_in(m):
    m.A = 0
    m.flags(m.A)


def _op_hlt(m):
    m.state = State.HALTED


def _op_movab(m):
    m.A = m.B
    m.flags(m.A)


def _op_movba(m):
    m.B = m.A


def _op_addc(m):
    _arith(m, m.A + m.C)


def _op_addd(m):
    _arith(m, m.A + m.D)


def _op_subc(m):
    _arith(m, m.A - m.C)


def _op_subd(m):
    _arith(m, m.A - m.D)


def _op_xor(m):
    m.A ^= m.B
    m.flags(m.A)


def _op_and(m):
    m.A &= m.B
    m.flags(m.A)


def _op_or(m):
    m.A |= m.B
    m.flags(m.A)


def _op_not(m):
    m.A = (~m.A) & 0xFFFF
    m.flags(m.A)


def _op_inp(m):
    port = m.fetch8()
    m.A = m.hardware.read_port(port) & 0xFFFF
    m.flags(m.A)


def _op_outp(m):
    port = m.fetch8()
    m.hardware.write_port(port, m.A)


def _op_trap(m):
    m._begin_trap()


def _op_stb(m):
    m.write8(m.B, m.A)


def _op_ldb(m):
    m.A = m.read8(m.B)
    m.flags(m.A)


def _op_stw(m):
    m.write16(m.B, m.A)


def _op_ldw(m):
    m.A = m.read16(m.B)
    m.flags(m.A)


def _op_incb(m):
    m.B = (m.B + 1) & 0xFFFF


def _op_decb(m):
    m.B = (m.B - 1) & 0xFFFF


def _op_incc(m):
    m.C = (m.C + 1) & 0xFFFF


def _op_decc(m):
    m.C = (m.C - 1) & 0xFFFF


def _op_movac(m):
    m.A = m.C
    m.flags(m.A)


def _op_movca(m):
    m.C = m.A


def _op_movad(m):
    m.A = m.D
    m.flags(m.A)


def _op_movda(m):
    m.D = m.A


def _op_jn(m):
    address = m.fetch16()
    if m.N:
        m.PC = address


def _op_jnn(m):
    address = m.fetch16()
    if not m.N:
        m.PC = address


def _op_jc(m):
    address = m.fetch16()
    if m.CF:
        m.PC = address


def _op_jnc(m):
    address = m.fetch16()
    if not m.CF:
        m.PC = address


def _op_mul(m):
    product = m.A * m.B
    m.CF = int(product > 0xFFFF)
    m.A = product & 0xFFFF
    m.flags(m.A)


def _op_div(m):
    if m.B == 0:
        m.CF = 1
        return
    m.CF = 0
    quotient, remainder = divmod(m.A, m.B)
    m.A = quotient & 0xFFFF
    m.D = remainder & 0xFFFF
    m.flags(m.A)


def _op_shl(m):
    m.CF = int(bool(m.A & 0x8000))
    m.A = (m.A << 1) & 0xFFFF
    m.flags(m.A)


def _op_shr(m):
    m.CF = m.A & 1
    m.A = (m.A >> 1) & 0xFFFF
    m.flags(m.A)


def _op_outs(m):
    text, _ = m.read_string(m.B)
    m.output_buffer.append(text.decode("utf-8", "replace"))


def _op_trapa(m):
    m._begin_async()


def _op_wfi(m):
    """Wait for interrupt. Deadlock is a fault, not a hang: a WFI that no
    event can ever satisfy is a guest bug and says so immediately."""
    if m.irq_pending:
        if not m.ien:
            raise CPUFault("WFI with the pending interrupt masked (CLI deadlock)")
        m._deliver_irq()
        return
    if m.async_pending is not None:
        # Park until the reply arrives; resume() will deliver it and the
        # IRQ fires at the next instruction boundary.
        m.pending = m.async_pending
        m.state = State.TRAPPED
        raise _Suspend()
    raise CPUFault("WFI with no interrupt source")


def _op_cli(m):
    m.ien = 0


def _op_sti(m):
    m.ien = 1


def _op_iret(m):
    m.PC = m.pop16()
    m.cs = m.pop16() & 1
    m.ien = 1


def _op_callx(m):
    address = m.fetch16()
    m.push16(m.cs)
    m.push16(m.PC)
    m.cs = 1
    m.PC = address


def _op_retx(m):
    m.PC = m.pop16()
    m.cs = m.pop16() & 1


_HANDLERS = {
    "NOP": _op_nop, "LDIA": _op_ldia, "LDIB": _op_ldib, "LDIC": _op_ldic,
    "LDID": _op_ldid, "ADD": _op_add, "SUB": _op_sub, "INC": _op_inc,
    "DEC": _op_dec, "STA": _op_sta, "LDA": _op_lda, "JMP": _op_jmp,
    "JZ": _op_jz, "JNZ": _op_jnz, "CMP": _op_cmp, "PUSH": _op_push,
    "POP": _op_pop, "CALL": _op_call, "RET": _op_ret, "OUT": _op_out,
    "IN": _op_in, "HLT": _op_hlt, "MOVAB": _op_movab, "MOVBA": _op_movba,
    "ADDC": _op_addc, "ADDD": _op_addd, "SUBC": _op_subc, "SUBD": _op_subd,
    "XOR": _op_xor, "AND": _op_and, "OR": _op_or, "NOT": _op_not,
    "INP": _op_inp, "OUTP": _op_outp, "TRAP": _op_trap, "STB": _op_stb,
    "LDB": _op_ldb, "STW": _op_stw, "LDW": _op_ldw, "INCB": _op_incb,
    "DECB": _op_decb, "INCC": _op_incc, "DECC": _op_decc, "MOVAC": _op_movac,
    "MOVCA": _op_movca, "MOVAD": _op_movad, "MOVDA": _op_movda, "JN": _op_jn,
    "JNN": _op_jnn, "JC": _op_jc, "JNC": _op_jnc, "MUL": _op_mul,
    "DIV": _op_div, "SHL": _op_shl, "SHR": _op_shr, "OUTS": _op_outs,
    "CMPC": _op_cmpc, "TRAPA": _op_trapa, "WFI": _op_wfi, "CLI": _op_cli,
    "STI": _op_sti, "IRET": _op_iret, "CALLX": _op_callx, "RETX": _op_retx,
}

_DISPATCH = {OPS[name]: handler for name, handler in _HANDLERS.items()}

_MISSING = set(OPS) - set(_HANDLERS)
if _MISSING:  # pragma: no cover - guards ISA/implementation drift
    raise RuntimeError(f"unimplemented opcodes: {sorted(_MISSING)}")
