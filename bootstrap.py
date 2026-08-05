"""TRAPCPU 1.0.0 - single file bootstrap.

A 16-bit computer with a language model as a memory-mapped coprocessor at I/O
port 0x30. Generated from the trapcpu package by tools/bundle.py; edit the
package, not this file.

Paste this whole file into a Python sandbox, then::

    machine = Machine()
    machine.load(assemble(SOURCE))
    result = machine.run()
    print(result.frame.render())        # publish this into the conversation
    result = machine.resume(REPLY)      # feed the model's next message back

Or drive it automatically with one of the built-in oracle backends::

    machine.execute(EchoOracle())
    print(machine.output())

Standard library only. No installation, no filesystem, no network.
"""

import argparse
import io
import os
import random
import re
import sys
from collections import Counter


# ==========================================================================
# MODULE: trapcpu/isa.py
# ==========================================================================

"""TRAPCPU instruction set.

TRAPCPU is a fork of ChatCPU. The base ISA (0x00-0x21) is unchanged so
existing ChatCPU programs still assemble and run. Everything from 0x22 up is
new, and exists for one reason: to make it practical to write assembly that
builds a prompt in RAM, hands it to the oracle coprocessor, and walks the
reply back out again.

Register conventions used throughout the machine:

    A   accumulator / primary operand / syscall status
    B   pointer register (all indirect access goes through B)
    C   counter
    D   scratch, and the destination for DIV remainders and NUM oracle results
"""

# ---------------------------------------------------------------------------
# I/O PORTS
# ---------------------------------------------------------------------------

PORT_KEY = 0x00        # read: pop a keycode off the input queue (0 if empty)
PORT_KEY_STATE = 0x01  # read: 1 if a keycode is queued
PORT_SCREEN = 0x10     # write: plot chr(A) at (B, C)
PORT_RANDOM = 0x20     # read: uniform byte
PORT_ORACLE = 0x30     # write: latch A as the oracle request descriptor pointer
PORT_ORACLE_STAT = 0x31  # read: status of the most recent completed trap

# ---------------------------------------------------------------------------
# OPCODES
# ---------------------------------------------------------------------------

OPS = {
    # --- base ChatCPU ISA -------------------------------------------------
    "NOP": 0x00,

    "LDIA": 0x01,   # A <- imm16
    "LDIB": 0x02,   # B <- imm16
    "LDIC": 0x03,   # C <- imm16
    "LDID": 0x04,   # D <- imm16

    "ADD": 0x05,    # A <- A + B
    "SUB": 0x06,    # A <- A - B
    "INC": 0x07,    # A <- A + 1
    "DEC": 0x08,    # A <- A - 1

    "STA": 0x09,    # RAM16[imm16] <- A
    "LDA": 0x0A,    # A <- RAM16[imm16]

    "JMP": 0x0B,
    "JZ": 0x0C,
    "JNZ": 0x0D,

    "CMP": 0x0E,    # flags from A - B, A unchanged

    "PUSH": 0x0F,   # push A
    "POP": 0x10,    # pop A

    "CALL": 0x11,
    "RET": 0x12,

    "OUT": 0x13,    # emit chr(A & 0xFF) to the console
    "IN": 0x14,     # A <- 0 (legacy no-op input)

    "HLT": 0x15,

    "MOVAB": 0x16,  # A <- B
    "MOVBA": 0x17,  # B <- A

    "ADDC": 0x18,   # A <- A + C
    "ADDD": 0x19,   # A <- A + D
    "SUBC": 0x1A,   # A <- A - C
    "SUBD": 0x1B,   # A <- A - D

    "XOR": 0x1C,
    "AND": 0x1D,
    "OR": 0x1E,
    "NOT": 0x1F,

    "INP": 0x20,    # A <- port[imm8]
    "OUTP": 0x21,   # port[imm8] <- A

    # --- TRAPCPU additions ------------------------------------------------
    "TRAP": 0x22,   # invoke the oracle coprocessor; suspends the machine

    "STB": 0x23,    # RAM8[B]  <- A & 0xFF
    "LDB": 0x24,    # A        <- RAM8[B]        (zero extended)
    "STW": 0x25,    # RAM16[B] <- A
    "LDW": 0x26,    # A        <- RAM16[B]

    "INCB": 0x27,
    "DECB": 0x28,
    "INCC": 0x29,
    "DECC": 0x2A,

    "MOVAC": 0x2B,  # A <- C
    "MOVCA": 0x2C,  # C <- A
    "MOVAD": 0x2D,  # A <- D
    "MOVDA": 0x2E,  # D <- A

    "JN": 0x2F,     # jump if N
    "JNN": 0x30,    # jump if not N
    "JC": 0x31,     # jump if CF
    "JNC": 0x32,    # jump if not CF

    "MUL": 0x33,    # A <- (A * B) & 0xFFFF, CF on overflow
    "DIV": 0x34,    # A <- A // B, D <- A % B; CF=1 and A unchanged if B == 0
    "SHL": 0x35,    # A <- A << 1, CF <- bit shifted out
    "SHR": 0x36,    # A <- A >> 1, CF <- bit shifted out

    "OUTS": 0x37,   # emit the NUL terminated string at RAM[B] to the console
    "CMPC": 0x38,   # flags from A - C, A unchanged
}

# Mnemonics that carry a one byte operand.
ARG8_OPS = frozenset({"INP", "OUTP"})

# Mnemonics that carry a little endian two byte operand.
ARG16_OPS = frozenset({
    "LDIA", "LDIB", "LDIC", "LDID",
    "STA", "LDA",
    "JMP", "JZ", "JNZ",
    "CALL",
    "JN", "JNN", "JC", "JNC",
})

MNEMONICS = {opcode: name for name, opcode in OPS.items()}


def width(mnemonic):
    """Encoded size in bytes of one instruction."""
    mnemonic = mnemonic.upper()
    if mnemonic in ARG8_OPS:
        return 2
    if mnemonic in ARG16_OPS:
        return 3
    return 1

# ==========================================================================
# MODULE: trapcpu/protocol.py
# ==========================================================================

"""The TRAP protocol: how a 16-bit machine talks to a language model.

This module owns the wire format and nothing else. It renders request frames,
parses reply frames, and runs the validation ladder. It has no knowledge of the
CPU and never touches RAM, which is deliberate: the protocol has to be testable
without a machine attached, because most of the interesting failures are
protocol failures.

Design notes worth knowing before reading the code:

* **Frame markers are anchored to column zero.** The request frame contains a
  copy of the reply template so the model has something to imitate, and that
  template is indented by two spaces. A request therefore cannot be mistaken
  for a reply by the parser, which matters a great deal once transcripts start
  getting re-read as storage.
* **A fresh nonce is issued per attempt, not per trap.** Retrying with the same
  nonce would let a stale reply from the previous attempt satisfy the retry.
* **LEN is mandatory, CRC is not.** LEN is the one integrity field a language
  model can produce unaided. CRC16/SUM are verified when present because
  tool-using or API-driven oracles can compute them, and their absence is
  recorded as DEGRADED rather than silently accepted.
* **Redundancy, not checksums, is the error correction story.** The transport is
  lossless; what corrupts a payload is the model, so the only meaningful ECC is
  to sample it more than once and vote. See :func:`vote`.
"""


PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# WIRE MARKERS
# ---------------------------------------------------------------------------

REQUEST_BEGIN = f"=== TRAPCPU/{PROTOCOL_VERSION} ORACLE REQUEST ==="
REPLY_BEGIN = f"=== TRAPCPU/{PROTOCOL_VERSION} ORACLE REPLY ==="
FRAME_END = "=== TRAPCPU END ==="

PROMPT_OPEN = "PROMPT>>>"
PROMPT_CLOSE = "<<<PROMPT"
PAYLOAD_OPEN = "PAYLOAD>>>"
PAYLOAD_CLOSE = "<<<PAYLOAD"

_REPLY_BEGIN_RE = re.compile(r"^===\s*TRAPCPU/(\d+)\s+ORACLE\s+REPLY\s*===\s*$")
_END_RE = re.compile(r"^===\s*TRAPCPU\s+END\s*===\s*$")
_HEADER_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*:\s*(.*)$")

# Lines a prompt is not allowed to emit, because they would forge frame
# structure. Colliding lines get one space of padding on the way out.
_RESERVED_RE = re.compile(
    r"^(?:===.*|(?:"
    + "|".join(
        re.escape(marker)
        for marker in (PROMPT_OPEN, PROMPT_CLOSE, PAYLOAD_OPEN, PAYLOAD_CLOSE)
    )
    + r")\s*)$"
)


# ---------------------------------------------------------------------------
# STATUS CODES
# ---------------------------------------------------------------------------

class Status:
    """Oracle completion codes, as written back to the descriptor and A."""

    OK = 0x00              # payload accepted, integrity fully verified
    DEGRADED = 0x01        # payload accepted, but no checksum was supplied

    NO_REQUEST = 0x10      # TRAP executed with no descriptor latched
    BAD_DESCRIPTOR = 0x11  # descriptor magic/version/bounds rejected
    NO_FRAME = 0x12        # reply contained no reply frame at all
    BAD_VERSION = 0x13     # frame version this machine does not speak
    BAD_NONCE = 0x14       # stale or mismatched nonce
    MALFORMED = 0x15       # structure broken: fences, blocks, status word
    BAD_LENGTH = 0x16      # declared LEN disagrees with the payload
    BAD_CHECKSUM = 0x17    # CRC/SUM mismatch, or missing under STRICT_CRC
    OVERFLOW = 0x18        # payload longer than the response buffer
    BAD_ENCODING = 0x19    # mode specific decode failure (NUM, BYTES)
    NO_QUORUM = 0x1A       # replicas disagreed with no strict majority

    REFUSED = 0x20         # the oracle declined to answer
    RETRIES = 0x21         # attempts exhausted
    BUDGET = 0x22          # oracle call budget exhausted
    ABORT = 0x23           # host aborted the trap


STATUS_NAMES = {
    value: name
    for name, value in vars(Status).items()
    if not name.startswith("_") and isinstance(value, int)
}


def status_name(code):
    return STATUS_NAMES.get(code, f"UNKNOWN_{code:02X}")


def is_success(code):
    return code in (Status.OK, Status.DEGRADED)


# ---------------------------------------------------------------------------
# MODES AND FLAGS
# ---------------------------------------------------------------------------

class Mode:
    TEXT = 0    # payload is written to the buffer as UTF-8 bytes plus a NUL
    NUM = 1     # payload must parse as an integer; stored as a 16-bit word
    BYTES = 2   # payload must be hex digit pairs; stored raw


MODE_NAMES = {Mode.TEXT: "TEXT", Mode.NUM: "NUM", Mode.BYTES: "BYTES"}
MODE_VALUES = {name: value for value, name in MODE_NAMES.items()}


class Flag:
    STRICT_CRC = 0x0001      # a reply without CRC/SUM is a failure, not DEGRADED
    NO_TRIM = 0x0002         # keep leading/trailing whitespace in the payload
    ALLOW_TRUNCATE = 0x0004  # clip an overlong payload instead of failing
    NO_NUL = 0x0008          # TEXT mode: do not append a terminator


# ---------------------------------------------------------------------------
# DESCRIPTOR ABI
# ---------------------------------------------------------------------------
# The oracle request descriptor is a 24 byte structure in RAM. A program builds
# one, writes its address to PORT_ORACLE, and executes TRAP. Fields marked OUT
# are written back by the emulator when the trap completes.

DESCRIPTOR_MAGIC = 0x524F  # 'O','R' little endian
DESCRIPTOR_SIZE = 0x18

FIELD_MAGIC = 0x00       # u16  'OR'
FIELD_VERSION = 0x02     # u8   protocol version
FIELD_MODE = 0x03        # u8   Mode
FIELD_PROMPT = 0x04      # u16  pointer to a NUL terminated prompt
FIELD_RESPONSE = 0x06    # u16  pointer to the response buffer
FIELD_CAPACITY = 0x08    # u16  response buffer capacity in bytes
FIELD_REPLICAS = 0x0A    # u8   1..MAX_REPLICAS independent samples
FIELD_RETRIES = 0x0B     # u8   protocol retries before giving up
FIELD_STATUS = 0x0C      # u16  OUT: Status
FIELD_LENGTH = 0x0E      # u16  OUT: bytes written to the response buffer
FIELD_NONCE = 0x10       # u16  OUT: nonce of the last attempt
FIELD_ATTEMPTS = 0x12    # u8   OUT: attempts consumed
FIELD_CORRECTED = 0x13   # u8   OUT: byte positions repaired by the vote
FIELD_FLAGS = 0x14       # u16  Flag bits
FIELD_VALUE = 0x16       # u16  OUT: decoded value in NUM mode

MAX_REPLICAS = 9
MAX_PROMPT = 4096
MAX_RETRIES = 15


def pack_descriptor(prompt, response, capacity, mode=Mode.TEXT, replicas=1,
                    retries=0, flags=0):
    """Build a 24 byte request descriptor. The assembly equivalent of this is
    the ``.DW``/``.DB`` block at the top of every demo program."""
    blob = bytearray(DESCRIPTOR_SIZE)

    def word(offset, value):
        blob[offset] = value & 0xFF
        blob[offset + 1] = (value >> 8) & 0xFF

    word(FIELD_MAGIC, DESCRIPTOR_MAGIC)
    blob[FIELD_VERSION] = PROTOCOL_VERSION
    blob[FIELD_MODE] = mode
    word(FIELD_PROMPT, prompt)
    word(FIELD_RESPONSE, response)
    word(FIELD_CAPACITY, capacity)
    blob[FIELD_REPLICAS] = replicas
    blob[FIELD_RETRIES] = retries
    word(FIELD_FLAGS, flags)
    return bytes(blob)


# ---------------------------------------------------------------------------
# CHECKSUMS
# ---------------------------------------------------------------------------

def crc16(data, init=0xFFFF):
    """CRC-16/CCITT-FALSE. Used for frames and for snapshot sectors."""
    crc = init
    for byte in data:
        crc ^= (byte << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def checksum_sum(data):
    """Additive checksum mod 2**16. Weak, but a model can compute it."""
    return sum(data) & 0xFFFF


# ---------------------------------------------------------------------------
# TEXT HELPERS
# ---------------------------------------------------------------------------

def escape_reserved(text):
    """Indent any line that would otherwise forge a frame marker.

    Prompt text comes from guest RAM, so a program could otherwise write a line
    reading ``=== TRAPCPU END ===`` into its own prompt and truncate the frame
    it is sitting inside. One leading space defeats the column zero anchors.
    """
    return "\n".join(
        " " + line if _RESERVED_RE.match(line) else line
        for line in text.split("\n")
    )


def _parse_int(text):
    text = text.strip()
    if not text:
        return None
    try:
        if text.lower().startswith("0x"):
            return int(text, 16)
        return int(text, 10)
    except ValueError:
        return None


def _parse_hex_word(text):
    text = text.strip().upper()
    if text.startswith("0X"):
        text = text[2:]
    if not text or len(text) > 4 or any(c not in "0123456789ABCDEF" for c in text):
        return None
    return int(text, 16)


# ---------------------------------------------------------------------------
# REQUEST FRAME
# ---------------------------------------------------------------------------

class TrapFrame:
    """Everything the host needs to publish one oracle request."""

    def __init__(self, nonce, prompt, mode=Mode.TEXT, replicas=1, capacity=64,
                 attempt=1, retries=0, flags=0, registers=None, cycle=0,
                 descriptor=0, last_status=None, last_detail=None):
        self.nonce = nonce & 0xFFFF
        self.prompt = prompt
        self.mode = mode
        self.replicas = max(1, replicas)
        self.capacity = capacity
        self.attempt = attempt
        self.retries = retries
        self.flags = flags
        self.registers = registers or {}
        self.cycle = cycle
        self.descriptor = descriptor
        self.last_status = last_status
        self.last_detail = last_detail

    @property
    def nonce_text(self):
        return f"{self.nonce:04X}"

    @property
    def mode_name(self):
        return MODE_NAMES.get(self.mode, f"MODE{self.mode}")

    def render(self):
        """Serialize the frame for publication into the conversation."""
        lines = [REQUEST_BEGIN]
        lines.append(f"NONCE: {self.nonce_text}")
        lines.append(f"ATTEMPT: {self.attempt}/{self.retries + 1}")
        lines.append(f"MODE: {self.mode_name}")
        lines.append(f"REPLICAS: {self.replicas}")
        lines.append(f"MAXLEN: {self.capacity}")
        lines.append(f"CYCLE: {self.cycle}")
        lines.append(f"DESC: {self.descriptor:04X}")

        if self.registers:
            lines.append("REGS: " + " ".join(
                f"{name}={value:04X}" if name in ("A", "B", "C", "D", "PC", "SP")
                else f"{name}={value}"
                for name, value in self.registers.items()
            ))

        if self.last_status is not None:
            detail = f" ({self.last_detail})" if self.last_detail else ""
            lines.append(
                f"RETRY_REASON: {status_name(self.last_status)}{detail}"
            )

        lines.append(PROMPT_OPEN)
        lines.append(escape_reserved(self.prompt))
        lines.append(PROMPT_CLOSE)
        lines.append("")
        lines.extend(self._instructions())
        lines.append(FRAME_END)
        return "\n".join(lines)

    def _instructions(self):
        """The reply contract, indented so it cannot parse as a real reply."""
        out = ["REPLY WITH ONE FRAME IN EXACTLY THIS FORM, AT COLUMN ZERO,",
               "WITH NO SURROUNDING PROSE AND NO CODE FENCES:", ""]

        pad = "  "
        out.append(pad + REPLY_BEGIN)
        out.append(pad + f"NONCE: {self.nonce_text}")
        out.append(pad + "STATUS: OK")

        for index in range(1, self.replicas + 1):
            if self.replicas > 1:
                out.append(pad + f"REPLICA: {index}")
            out.append(pad + "LEN: <payload length in bytes>")
            out.append(pad + PAYLOAD_OPEN)
            out.append(pad + "<answer>")
            out.append(pad + PAYLOAD_CLOSE)

        out.append(pad + FRAME_END)
        out.append("")
        out.extend(self._rules())
        return out

    def _rules(self):
        rules = [
            "RULES:",
            f"- NONCE must be exactly {self.nonce_text}. Any other value is "
            "rejected as a stale reply.",
            "- LEN is the number of UTF-8 bytes in the payload after leading "
            "and trailing whitespace is stripped."
            if not self.flags & Flag.NO_TRIM else
            "- LEN is the number of UTF-8 bytes in the payload exactly as "
            "written between the fences.",
            f"- The payload must be at most {self.capacity} bytes.",
        ]

        if self.mode == Mode.NUM:
            rules.append(
                "- MODE is NUM: the payload must be a single integer in "
                "0..65535, digits only, no words, no units, no sign."
            )
        elif self.mode == Mode.BYTES:
            rules.append(
                "- MODE is BYTES: the payload must be an even number of "
                "hexadecimal digits and nothing else."
            )
        else:
            rules.append(
                "- MODE is TEXT: the payload is written into memory verbatim. "
                "Answer with the value only, no explanation."
            )

        if self.replicas > 1:
            rules.append(
                f"- REPLICAS is {self.replicas}: answer the question "
                f"{self.replicas} times independently, one payload block each, "
                "in the order shown. Do not copy one block into the others; "
                "the host takes a majority vote and identical blocks defeat it."
            )

        if self.flags & Flag.STRICT_CRC:
            rules.append(
                "- A CRC or SUM line is REQUIRED next to each LEN. "
                "CRC is CRC-16/CCITT-FALSE over the payload bytes as four "
                "uppercase hex digits; SUM is the sum of the payload bytes "
                "modulo 65536 in decimal."
            )
        else:
            rules.append(
                "- CRC/SUM lines are optional. Include one only if you can "
                "compute it exactly; a wrong checksum fails the trap, an "
                "absent one only downgrades it."
            )

        rules.append(
            "- If you will not answer, reply with the same frame and "
            "STATUS: REFUSED and no payload blocks."
        )
        return rules

    def __repr__(self):
        return (
            f"<TrapFrame nonce={self.nonce_text} mode={self.mode_name} "
            f"attempt={self.attempt}/{self.retries + 1} "
            f"replicas={self.replicas} cap={self.capacity}>"
        )


# ---------------------------------------------------------------------------
# REPLY PARSING
# ---------------------------------------------------------------------------

class Block:
    """One payload block inside a reply frame."""

    __slots__ = ("text", "declared_length", "crc", "sum", "replica", "closed")

    def __init__(self, text, declared_length=None, crc=None, checksum=None,
                 replica=None, closed=True):
        self.text = text
        self.declared_length = declared_length
        self.crc = crc
        self.sum = checksum
        self.replica = replica
        self.closed = closed


class RawFrame:
    """A syntactically located reply frame, not yet validated."""

    def __init__(self, version, nonce, status, blocks, terminated, line):
        self.version = version
        self.nonce = nonce
        self.status = status
        self.blocks = blocks
        self.terminated = terminated
        self.line = line


def find_reply_frames(text):
    """Locate every reply frame in a blob of text, in order of appearance."""
    lines = text.splitlines()
    frames = []
    index = 0

    while index < len(lines):
        match = _REPLY_BEGIN_RE.match(lines[index])
        if not match:
            index += 1
            continue

        version = int(match.group(1))
        start = index
        body = []
        terminated = False
        index += 1

        while index < len(lines):
            if _END_RE.match(lines[index]):
                terminated = True
                index += 1
                break
            if _REPLY_BEGIN_RE.match(lines[index]):
                # A new frame started before this one closed; the first one is
                # truncated. Leave the cursor here so the next one is seen too.
                break
            body.append(lines[index])
            index += 1

        frames.append(_parse_body(version, body, terminated, start + 1))

    return frames


def _parse_body(version, body, terminated, line):
    """Split a frame body into frame headers and payload blocks."""
    nonce = None
    status = None
    pending = {}
    blocks = []

    cursor = 0
    while cursor < len(body):
        raw = body[cursor]

        if raw.strip() == PAYLOAD_OPEN:
            collected = []
            cursor += 1
            closed = False
            while cursor < len(body):
                if body[cursor].strip() == PAYLOAD_CLOSE:
                    closed = True
                    cursor += 1
                    break
                collected.append(body[cursor])
                cursor += 1

            blocks.append(Block(
                text="\n".join(collected),
                declared_length=pending.get("LEN"),
                crc=pending.get("CRC"),
                checksum=pending.get("SUM"),
                replica=pending.get("REPLICA"),
                closed=closed,
            ))
            pending = {}
            continue

        header = _HEADER_RE.match(raw.strip())
        if header:
            key = header.group(1).upper()
            value = header.group(2).strip()
            if key == "NONCE" and nonce is None:
                nonce = value
            elif key == "STATUS" and status is None:
                status = value.upper()
            else:
                pending[key] = value

        cursor += 1

    return RawFrame(version, nonce, status, blocks, terminated, line)


# ---------------------------------------------------------------------------
# VOTING
# ---------------------------------------------------------------------------

class VoteResult:
    """Outcome of a replica election. ``ok`` false means uncorrectable."""

    __slots__ = ("payload", "corrected", "discarded", "unanimous", "reason")

    def __init__(self, payload=b"", corrected=0, discarded=0, unanimous=False,
                 reason=""):
        self.payload = payload
        self.corrected = corrected
        self.discarded = discarded
        self.unanimous = unanimous
        self.reason = reason

    @property
    def ok(self):
        return not self.reason


def vote(payloads):
    """Elect a payload from N independent samples, byte by byte.

    Two rounds, both requiring a **strict majority**, never a plurality:

    1. Length. A replica that disagrees about how long the answer is has
       answered a different question; folding it into the byte vote only
       smears the result, so those replicas are discarded first.
    2. Each byte position among the survivors.

    A position where no byte holds a majority is an uncorrectable error and
    fails the whole election, exactly as an ECC word with too many flipped bits
    does. Note the consequence for ``replicas=2``: disagreement is always
    detectable and never correctable, which is what a mirrored pair buys you.
    """
    if not payloads:
        return VoteResult(reason="no payloads")
    if len(payloads) == 1:
        return VoteResult(payloads[0], 0, 0, True)

    total = len(payloads)
    lengths = Counter(len(item) for item in payloads)
    length, agreement = lengths.most_common(1)[0]
    if agreement * 2 <= total:
        seen = sorted(lengths)
        return VoteResult(
            reason=f"no majority on payload length across {total} replicas "
                   f"(lengths {seen})"
        )

    candidates = [item for item in payloads if len(item) == length]
    discarded = total - len(candidates)

    elected = bytearray()
    corrected = 0
    for position in range(length):
        column = Counter(item[position] for item in candidates)
        byte, votes = column.most_common(1)[0]
        if votes * 2 <= len(candidates):
            return VoteResult(
                reason=f"no majority at byte {position} across "
                       f"{len(candidates)} replicas "
                       f"({votes}/{len(candidates)} for the leader)"
            )
        elected.append(byte)
        if votes != len(candidates):
            corrected += 1

    return VoteResult(
        bytes(elected), corrected, discarded,
        corrected == 0 and discarded == 0,
    )


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------

class OracleResult:
    """Outcome of validating one reply against one request."""

    def __init__(self, status, payload=b"", value=None, corrected=0,
                 discarded=0, detail="", notes=None):
        self.status = status
        self.payload = payload
        self.value = value
        self.corrected = corrected
        self.discarded = discarded
        self.detail = detail
        self.notes = notes or []

    @property
    def ok(self):
        return is_success(self.status)

    def __repr__(self):
        return (
            f"<OracleResult {status_name(self.status)} "
            f"len={len(self.payload)} corrected={self.corrected}"
            + (f" detail={self.detail!r}" if self.detail else "")
            + ">"
        )


def _fail(status, detail, notes):
    return OracleResult(status, detail=detail, notes=notes)


def validate(reply_text, frame):
    """Run the validation ladder for ``reply_text`` against ``frame``.

    Layers, in order, each with its own status code so a failure names itself:

    ``L0`` framing, ``L1`` addressing (nonce), ``L2`` declared length,
    ``L3`` checksum, ``L4`` redundancy vote, ``L5`` mode decode,
    ``L6`` capacity.
    """
    notes = []

    # -- L0: framing -------------------------------------------------------
    frames = find_reply_frames(reply_text or "")
    if not frames:
        return _fail(Status.NO_FRAME, "no reply frame found", notes)

    matching = [f for f in frames if _nonce_matches(f.nonce, frame.nonce)]
    if len(frames) > 1:
        notes.append(f"{len(frames)} reply frames present")
    raw = matching[-1] if matching else frames[-1]

    if not raw.terminated:
        return _fail(Status.MALFORMED, "frame not terminated", notes)

    if raw.version != PROTOCOL_VERSION:
        return _fail(
            Status.BAD_VERSION,
            f"frame speaks v{raw.version}, machine speaks v{PROTOCOL_VERSION}",
            notes,
        )

    # -- L1: addressing ----------------------------------------------------
    if raw.nonce is None:
        return _fail(Status.BAD_NONCE, "no NONCE header", notes)
    if not _nonce_matches(raw.nonce, frame.nonce):
        return _fail(
            Status.BAD_NONCE,
            f"nonce {raw.nonce.strip()} != {frame.nonce_text}",
            notes,
        )

    if raw.status in ("REFUSED", "REFUSE", "DECLINED"):
        return _fail(Status.REFUSED, "oracle declined", notes)
    if raw.status not in (None, "OK"):
        return _fail(Status.MALFORMED, f"unknown STATUS: {raw.status}", notes)

    open_blocks = [b for b in raw.blocks if not b.closed]
    if open_blocks:
        return _fail(Status.MALFORMED, "unterminated payload fence", notes)

    if not raw.blocks:
        return _fail(Status.MALFORMED, "reply contains no payload block", notes)

    if len(raw.blocks) != frame.replicas:
        return _fail(
            Status.MALFORMED,
            f"expected {frame.replicas} payload block(s), got {len(raw.blocks)}",
            notes,
        )

    # -- L2 and L3: per block length and checksum --------------------------
    payloads = []
    unchecked = 0

    for index, block in enumerate(raw.blocks, start=1):
        text = block.text
        if not frame.flags & Flag.NO_TRIM:
            text = text.strip()
        payload = text.encode("utf-8")

        declared = _parse_int(block.declared_length or "")
        if declared is None:
            return _fail(
                Status.BAD_LENGTH, f"block {index}: missing or unparsable LEN", notes
            )
        if declared != len(payload):
            return _fail(
                Status.BAD_LENGTH,
                f"block {index}: LEN {declared} != {len(payload)} actual",
                notes,
            )

        verified = False
        if block.crc is not None:
            declared_crc = _parse_hex_word(block.crc)
            if declared_crc is None:
                return _fail(
                    Status.BAD_CHECKSUM, f"block {index}: unparsable CRC", notes
                )
            actual = crc16(payload)
            if declared_crc != actual:
                return _fail(
                    Status.BAD_CHECKSUM,
                    f"block {index}: CRC {declared_crc:04X} != {actual:04X}",
                    notes,
                )
            verified = True

        if block.sum is not None:
            declared_sum = _parse_int(block.sum)
            if declared_sum is None:
                return _fail(
                    Status.BAD_CHECKSUM, f"block {index}: unparsable SUM", notes
                )
            actual = checksum_sum(payload)
            if declared_sum != actual:
                return _fail(
                    Status.BAD_CHECKSUM,
                    f"block {index}: SUM {declared_sum} != {actual}",
                    notes,
                )
            verified = True

        if not verified:
            unchecked += 1
            if frame.flags & Flag.STRICT_CRC:
                return _fail(
                    Status.BAD_CHECKSUM,
                    f"block {index}: no checksum under STRICT_CRC",
                    notes,
                )

        payloads.append(payload)

    # -- L4: redundancy ----------------------------------------------------
    election = vote(payloads)
    if not election.ok:
        return _fail(Status.NO_QUORUM, election.reason, notes)

    payload = election.payload
    if election.corrected:
        notes.append(f"vote repaired {election.corrected} byte(s)")
    if election.discarded:
        notes.append(f"vote discarded {election.discarded} replica(s) on length")

    # -- L5: mode decode ---------------------------------------------------
    value = None
    if frame.mode == Mode.NUM:
        text = payload.decode("utf-8", "replace").strip()
        parsed = _parse_int(text)
        if parsed is None:
            return _fail(
                Status.BAD_ENCODING, f"NUM payload not an integer: {text!r}", notes
            )
        if not 0 <= parsed <= 0xFFFF:
            return _fail(
                Status.BAD_ENCODING, f"NUM payload out of range: {parsed}", notes
            )
        value = parsed
        payload = bytes([parsed & 0xFF, (parsed >> 8) & 0xFF])

    elif frame.mode == Mode.BYTES:
        text = "".join(payload.decode("utf-8", "replace").split())
        if len(text) % 2 or any(c not in "0123456789abcdefABCDEF" for c in text):
            return _fail(
                Status.BAD_ENCODING, "BYTES payload is not hex digit pairs", notes
            )
        payload = bytes.fromhex(text)

    # -- L6: capacity ------------------------------------------------------
    if len(payload) > frame.capacity:
        if not frame.flags & Flag.ALLOW_TRUNCATE:
            return _fail(
                Status.OVERFLOW,
                f"payload {len(payload)}B exceeds buffer {frame.capacity}B",
                notes,
            )
        payload = payload[:frame.capacity]
        notes.append("payload truncated to buffer capacity")

    status = Status.DEGRADED if unchecked else Status.OK
    if unchecked:
        notes.append(f"{unchecked} block(s) carried no checksum")

    return OracleResult(
        status,
        payload=payload,
        value=value,
        corrected=election.corrected,
        discarded=election.discarded,
        notes=notes,
    )


def _nonce_matches(text, nonce):
    if text is None:
        return False
    parsed = _parse_hex_word(text)
    return parsed is not None and parsed == (nonce & 0xFFFF)


# ---------------------------------------------------------------------------
# REPLY CONSTRUCTION
# ---------------------------------------------------------------------------

def render_reply(nonce, payloads, status="OK", checksum=None, replicas=None,
                 trim=True):
    """Build a well formed reply frame. Used by non-human oracle backends.

    ``checksum`` may be ``None``, ``"crc"`` or ``"sum"``.
    """
    if isinstance(payloads, (str, bytes)):
        payloads = [payloads]

    lines = [REPLY_BEGIN, f"NONCE: {nonce & 0xFFFF:04X}", f"STATUS: {status}"]

    for index, item in enumerate(payloads, start=1):
        text = item.decode("utf-8") if isinstance(item, bytes) else str(item)
        canonical = text.strip() if trim else text
        blob = canonical.encode("utf-8")

        if replicas is not None and replicas > 1:
            lines.append(f"REPLICA: {index}")
        lines.append(f"LEN: {len(blob)}")
        if checksum == "crc":
            lines.append(f"CRC: {crc16(blob):04X}")
        elif checksum == "sum":
            lines.append(f"SUM: {checksum_sum(blob)}")
        lines.append(PAYLOAD_OPEN)
        lines.append(canonical)
        lines.append(PAYLOAD_CLOSE)

    lines.append(FRAME_END)
    return "\n".join(lines)

# ==========================================================================
# MODULE: trapcpu/assembler.py
# ==========================================================================

"""Two pass assembler for TRAPCPU.

The base ChatCPU assembler could only emit instructions, which is fine right
up to the moment you want to hand the oracle a prompt: prompts are data, and
there was no way to get a byte into RAM other than computing it. TRAPCPU adds
a DATA section. Code assembles into ROM, data assembles into RAM, and the
loader blits the data segments into RAM before execution starts.

Syntax
------

    .CODE                     switch to the code section (ROM). Default.
    .DATA                     switch to the data section (RAM).
    .ORG 0x0300               set the emit address of the current section
    .EQU NAME, expr           define a constant
    .DB  1, 2, 'x'            emit bytes
    .DW  0x1234, LABEL        emit little endian words
    .ASCII  "hi"              emit raw bytes
    .ASCIIZ "hi"              emit raw bytes plus a NUL terminator
    .RESB 64                  reserve N zero bytes
    .ALIGN 2                  pad with zeros up to a multiple of N
    .INCLUDE "trap.inc"       splice in another source file

Labels are ``NAME:`` and may share a line with an instruction. Operands accept
decimal, ``0x``/``0b``/``0o`` literals, ``'c'`` character literals, labels, and
``+``/``-`` chains of those (``BUFFER+2``, ``END-START``). Comments start at an
unquoted ``;``.
"""




MAX_INCLUDE_DEPTH = 8

SECTION_CODE = "CODE"
SECTION_DATA = "DATA"

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


class AssemblyError(Exception):
    """Raised for any source level problem, with a source location attached."""

    def __init__(self, message, lineno=None, text=None, filename=None):
        self.lineno = lineno
        self.text = text
        self.filename = filename
        if lineno is not None:
            where = f"{filename}:{lineno}" if filename else f"line {lineno}"
            message = f"{where}: {message}"
            if text:
                message += f"\n    {text.strip()}"
        super().__init__(message)


class Program:
    """Assembler output: a ROM image plus RAM segments to preload."""

    def __init__(self, code, data, symbols, origin=0):
        self.code = bytes(code)
        self.data = [(addr, bytes(blob)) for addr, blob in data]
        self.symbols = dict(symbols)
        self.origin = origin

    @property
    def size(self):
        return len(self.code) + sum(len(blob) for _, blob in self.data)

    def __repr__(self):
        return (
            f"<Program code={len(self.code)}B "
            f"data={sum(len(b) for _, b in self.data)}B "
            f"segments={len(self.data)} symbols={len(self.symbols)}>"
        )


# ---------------------------------------------------------------------------
# LEXING HELPERS
# ---------------------------------------------------------------------------

def strip_comment(line):
    """Remove a trailing ``;`` comment without cutting inside a quoted run."""
    quote = None
    escaped = False

    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ";":
            return line[:index]

    return line


def split_operands(text):
    """Split an operand list on commas, ignoring commas inside quotes."""
    operands = []
    current = []
    quote = None
    escaped = False

    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote is not None:
            current.append(char)
            escaped = True
            continue
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char == ",":
            operands.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        operands.append(tail)

    if quote is not None:
        raise AssemblyError("unterminated string literal")

    return operands


def split_label(line):
    """Peel a leading ``LABEL:`` off a line, quote aware."""
    quote = None
    escaped = False

    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ":":
            return line[:index].strip(), line[index + 1:].strip()

    return None, line.strip()


def unescape(literal):
    """Decode the body of a quoted literal."""
    out = []
    index = 0

    while index < len(literal):
        char = literal[index]
        if char == "\\":
            index += 1
            if index >= len(literal):
                raise AssemblyError("trailing backslash in string literal")
            code = literal[index]
            if code == "x":
                digits = literal[index + 1:index + 3]
                if len(digits) != 2:
                    raise AssemblyError("truncated \\x escape")
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    raise AssemblyError(f"bad \\x escape: \\x{digits}") from None
                index += 3
                continue
            if code not in _ESCAPES:
                raise AssemblyError(f"unknown escape: \\{code}")
            out.append(_ESCAPES[code])
            index += 1
            continue
        out.append(char)
        index += 1

    return "".join(out)


def string_operand(operand):
    """Decode a double quoted string operand into bytes."""
    operand = operand.strip()
    if len(operand) < 2 or operand[0] != '"' or operand[-1] != '"':
        raise AssemblyError(f"expected a quoted string, got {operand!r}")
    return unescape(operand[1:-1]).encode("utf-8")


# ---------------------------------------------------------------------------
# EXPRESSIONS
# ---------------------------------------------------------------------------

def parse_number(token):
    """Parse a single numeric or character term. Returns None if it is not one."""
    token = token.strip()
    if not token:
        return None

    lowered = token.lower()
    try:
        if lowered.startswith("0x"):
            return int(token, 16)
        if lowered.startswith("0b"):
            return int(token, 2)
        if lowered.startswith("0o"):
            return int(token, 8)
        if lowered.startswith("$"):
            return int(token[1:], 16)
    except ValueError:
        raise AssemblyError(f"bad numeric literal: {token!r}") from None

    if len(token) >= 3 and token[0] == "'" and token[-1] == "'":
        decoded = unescape(token[1:-1])
        if len(decoded) != 1:
            raise AssemblyError(
                f"character literal must be exactly one character: {token!r}"
            )
        return ord(decoded)

    try:
        return int(token, 10)
    except ValueError:
        return None


def _tokenize_expression(expression):
    """Split ``A+B-C`` into signed terms, leaving char literals intact."""
    terms = []
    current = []
    sign = 1
    quote = False
    escaped = False

    for char in expression:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                quote = False
            continue
        if char == "'":
            quote = True
            current.append(char)
            continue
        if char in "+-" and current and "".join(current).strip():
            terms.append((sign, "".join(current).strip()))
            current = []
            sign = 1 if char == "+" else -1
            continue
        if char in "+-" and not "".join(current).strip():
            # leading sign
            if char == "-":
                sign = -sign
            current = []
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        terms.append((sign, tail))

    return terms


def evaluate(expression, symbols, seen=None):
    """Evaluate an operand expression against a symbol table."""
    expression = expression.strip()
    if not expression:
        raise AssemblyError("empty expression")

    total = 0
    for sign, term in _tokenize_expression(expression):
        value = parse_number(term)
        if value is None:
            key = term.upper()
            if key not in symbols:
                raise AssemblyError(f"undefined symbol: {term}")
            resolved = symbols[key]
            if isinstance(resolved, str):
                seen = set() if seen is None else seen
                if key in seen:
                    raise AssemblyError(f"circular constant: {term}")
                resolved = evaluate(resolved, symbols, seen | {key})
            value = resolved
        total += sign * value

    return total


# ---------------------------------------------------------------------------
# SEGMENTS
# ---------------------------------------------------------------------------

class _Segment:
    """A run of bytes destined for one address space."""

    def __init__(self, base):
        self.base = base
        self.blob = bytearray()

    @property
    def end(self):
        return self.base + len(self.blob)


class _Emitter:
    """Address tracking byte sink that merges contiguous writes."""

    def __init__(self, base=0):
        self.address = base
        self.segments = []

    def seek(self, address):
        self.address = address

    def emit(self, data):
        if not data:
            return
        if self.segments and self.segments[-1].end == self.address:
            self.segments[-1].blob.extend(data)
        else:
            segment = _Segment(self.address)
            segment.blob.extend(data)
            self.segments.append(segment)
        self.address += len(data)

    def skip(self, count):
        """Advance without emitting; the gap stays zero filled."""
        self.address += count

    def as_list(self):
        return [(s.base, bytes(s.blob)) for s in self.segments if s.blob]

    def flat(self):
        """Flatten to a single image starting at the lowest base address."""
        entries = self.as_list()
        if not entries:
            return 0, b""
        base = min(addr for addr, _ in entries)
        end = max(addr + len(blob) for addr, blob in entries)
        image = bytearray(end - base)
        for addr, blob in entries:
            image[addr - base:addr - base + len(blob)] = blob
        return base, bytes(image)




# ---------------------------------------------------------------------------
# SOURCE LOCATIONS AND INCLUDES
# ---------------------------------------------------------------------------

class Loc:
    """Where a line came from, after includes are spliced in."""

    __slots__ = ("filename", "lineno")

    def __init__(self, filename, lineno):
        self.filename = filename
        self.lineno = lineno

    def __repr__(self):
        return f"{self.filename}:{self.lineno}"


def _at(message, loc, raw):
    """Attach a source location to an error."""
    return AssemblyError(str(message), loc.lineno, raw, loc.filename)


_INCLUDE_PREFIX = ".INCLUDE"


def expand_includes(source, filename="<source>", search_paths=(), depth=0,
                    stack=()):
    """Flatten ``.INCLUDE`` directives into a list of ``(Loc, text)`` lines."""
    if depth > MAX_INCLUDE_DEPTH:
        raise AssemblyError(
            f"include nesting deeper than {MAX_INCLUDE_DEPTH} in {filename}"
        )

    units = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        loc = Loc(filename, lineno)
        stripped = strip_comment(raw).strip()

        if not stripped.upper().startswith(_INCLUDE_PREFIX):
            units.append((loc, raw))
            continue

        head = stripped.split(None, 1)
        if head[0].upper() != _INCLUDE_PREFIX:
            units.append((loc, raw))
            continue
        if len(head) != 2:
            raise _at(".INCLUDE takes a quoted filename", loc, raw)

        try:
            target = string_operand(head[1].strip()).decode("utf-8")
        except AssemblyError as error:
            raise _at(error, loc, raw) from None

        resolved = _resolve_include(target, filename, search_paths)
        if resolved is None:
            raise _at(f"cannot find include {target!r}", loc, raw)
        if resolved in stack:
            raise _at(f"circular include of {target!r}", loc, raw)

        with open(resolved, "r", encoding="utf-8") as handle:
            nested = handle.read()

        units.extend(expand_includes(
            nested, resolved, search_paths, depth + 1, stack + (resolved,)
        ))

    return units


def _resolve_include(target, filename, search_paths):
    candidates = []
    if os.path.isabs(target):
        candidates.append(target)
    else:
        base = os.path.dirname(filename)
        if base:
            candidates.append(os.path.join(base, target))
        candidates.append(target)
        candidates.extend(os.path.join(path, target) for path in search_paths)

    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None


# ---------------------------------------------------------------------------
# ASSEMBLER
# ---------------------------------------------------------------------------

class _Line:
    __slots__ = ("loc", "raw", "section", "address", "op", "operands")

    def __init__(self, loc, raw, section, address, op, operands):
        self.loc = loc
        self.raw = raw
        self.section = section
        self.address = address
        self.op = op
        self.operands = operands


def _parse_source(units):
    """Split located lines into ``(loc, raw, label, op, operands)`` records."""
    records = []

    for loc, raw in units:
        try:
            line = strip_comment(raw).strip()
        except AssemblyError as error:
            raise _at(error, loc, raw) from None

        if not line:
            continue

        try:
            label, rest = split_label(line)
        except AssemblyError as error:
            raise _at(error, loc, raw) from None

        if label is not None and not label:
            raise _at("empty label", loc, raw)

        op = None
        operands = []
        if rest:
            # Directives and mnemonics never contain whitespace, so the first
            # whitespace run separates the opcode from its operand list.
            head = rest.split(None, 1)
            op = head[0].upper()
            tail = head[1] if len(head) > 1 else ""
            try:
                operands = split_operands(tail)
            except AssemblyError as error:
                raise _at(error, loc, raw) from None

        records.append((loc, raw, label, op, operands))

    return records


def _sizeof(op, operands, symbols, loc, raw):
    """Pass one size of a directive or instruction, in bytes."""
    if op in ARG8_OPS:
        return 2
    if op in ARG16_OPS:
        return 3
    if op in OPS:
        return 1

    if op == ".DB":
        return len(operands)
    if op == ".DW":
        return 2 * len(operands)
    if op in (".ASCII", ".ASCIIZ"):
        total = 0
        for operand in operands:
            try:
                total += len(string_operand(operand))
            except AssemblyError as error:
                raise _at(error, loc, raw) from None
        return total + (1 if op == ".ASCIIZ" else 0)
    if op == ".RESB":
        if len(operands) != 1:
            raise _at(".RESB takes one operand", loc, raw)
        try:
            count = evaluate(operands[0], symbols)
        except AssemblyError as error:
            raise _at(
                f"{error} (.RESB size must resolve during pass one)", loc, raw
            ) from None
        if count < 0:
            raise _at(".RESB size must not be negative", loc, raw)
        return count

    raise _at(f"unknown instruction or directive: {op}", loc, raw)


def assemble(source, filename="<source>", search_paths=()):
    """Assemble TRAPCPU source into a :class:`Program`."""
    units = expand_includes(source, filename, search_paths)
    records = _parse_source(units)

    symbols = {}
    addresses = {SECTION_CODE: 0, SECTION_DATA: 0}
    section = SECTION_CODE
    layout = []

    # ---- pass one: addresses, labels, constants --------------------------
    for loc, raw, label, op, operands in records:
        if label:
            key = label.upper()
            if key in symbols:
                raise _at(f"duplicate symbol: {label}", loc, raw)
            symbols[key] = addresses[section]

        if op is None:
            continue

        if op == ".CODE":
            section = SECTION_CODE
            continue

        if op == ".DATA":
            section = SECTION_DATA
            continue

        if op == ".ORG":
            if len(operands) != 1:
                raise _at(".ORG takes one operand", loc, raw)
            try:
                addresses[section] = evaluate(operands[0], symbols) & 0xFFFF
            except AssemblyError as error:
                raise _at(
                    f"{error} (.ORG address must resolve during pass one)",
                    loc, raw,
                ) from None
            if label:
                symbols[label.upper()] = addresses[section]
            continue

        if op == ".EQU":
            if len(operands) != 2:
                raise _at(".EQU takes NAME, value", loc, raw)
            name = operands[0].strip().upper()
            if not name:
                raise _at(".EQU needs a name", loc, raw)
            if name in symbols:
                raise _at(f"duplicate symbol: {operands[0]}", loc, raw)
            try:
                symbols[name] = evaluate(operands[1], symbols)
            except AssemblyError:
                # Forward references are allowed; resolve them in pass two.
                symbols[name] = operands[1]
            continue

        if op == ".ALIGN":
            if len(operands) != 1:
                raise _at(".ALIGN takes one operand", loc, raw)
            try:
                boundary = evaluate(operands[0], symbols)
            except AssemblyError as error:
                raise _at(
                    f"{error} (.ALIGN must resolve during pass one)", loc, raw
                ) from None
            if boundary <= 0:
                raise _at(".ALIGN needs a positive boundary", loc, raw)
            addresses[section] += (-addresses[section]) % boundary
            continue

        size = _sizeof(op, operands, symbols, loc, raw)
        layout.append(_Line(loc, raw, section, addresses[section], op, operands))
        addresses[section] += size

    # ---- pass two: emit --------------------------------------------------
    code = _Emitter()
    data = _Emitter()
    emitters = {SECTION_CODE: code, SECTION_DATA: data}

    for line in layout:
        emitter = emitters[line.section]
        emitter.seek(line.address)
        op = line.op
        operands = line.operands

        try:
            if op == ".DB":
                blob = bytearray()
                for operand in operands:
                    value = evaluate(operand, symbols)
                    if not 0 <= value <= 0xFF:
                        raise AssemblyError(f".DB value out of range: {value}")
                    blob.append(value)
                emitter.emit(blob)
                continue

            if op == ".DW":
                blob = bytearray()
                for operand in operands:
                    value = evaluate(operand, symbols) & 0xFFFF
                    blob.append(value & 0xFF)
                    blob.append((value >> 8) & 0xFF)
                emitter.emit(blob)
                continue

            if op in (".ASCII", ".ASCIIZ"):
                blob = bytearray()
                for operand in operands:
                    blob.extend(string_operand(operand))
                if op == ".ASCIIZ":
                    blob.append(0)
                emitter.emit(blob)
                continue

            if op == ".RESB":
                emitter.skip(evaluate(operands[0], symbols))
                continue

            blob = bytearray([OPS[op]])

            if op in ARG8_OPS:
                if len(operands) != 1:
                    raise AssemblyError(f"{op} takes one operand")
                value = evaluate(operands[0], symbols)
                if not 0 <= value <= 0xFF:
                    raise AssemblyError(f"{op} operand out of range: {value}")
                blob.append(value)
            elif op in ARG16_OPS:
                if len(operands) != 1:
                    raise AssemblyError(f"{op} takes one operand")
                value = evaluate(operands[0], symbols) & 0xFFFF
                blob.append(value & 0xFF)
                blob.append((value >> 8) & 0xFF)
            elif operands:
                raise AssemblyError(f"{op} takes no operands")

            emitter.emit(blob)

        except AssemblyError as error:
            raise _at(error, line.loc, line.raw) from None

    origin, image = code.flat()
    if origin and image:
        # ROM is loaded at zero, so a code .ORG shifts the image, not the base.
        image = bytes(bytearray(origin) + bytearray(image))

    resolved = {}
    for name, value in symbols.items():
        resolved[name] = evaluate(value, symbols) if isinstance(value, str) else value

    return Program(image, data.as_list(), resolved)


def assemble_file(path, search_paths=()):
    """Assemble a file, resolving includes relative to it."""
    with open(path, "r", encoding="utf-8") as handle:
        return assemble(handle.read(), filename=path, search_paths=search_paths)

# ==========================================================================
# MODULE: trapcpu/oracle.py
# ==========================================================================

"""Oracle backends: the things that can sit at port 0x30.

An oracle is anything with ``ask(frame) -> str | None``. It receives a rendered
:class:`~trapcpu.protocol.TrapFrame` and returns raw reply text, which the
machine then validates. Returning ``None`` means "I could not answer", and the
machine completes the trap with ``Status.RETRIES``.

The backends here divide into two families:

* real oracles (:class:`ManualOracle`, :class:`CallbackOracle`) which put an
  actual model or human in the loop, and
* test oracles (:class:`ScriptedOracle`, :class:`EchoOracle`,
  :class:`FaultInjector`) which exist so the deterministic scaffolding can be
  tested without a nondeterministic part attached.

:class:`FaultInjector` is the important one. Every fault it can inject is a
failure mode observed from real models: dropping the frame, answering the
previous question, padding the payload with prose, miscounting bytes, silently
truncating, or refusing.
"""





class Oracle:
    """Base class. Subclasses implement :meth:`ask`."""

    name = "oracle"

    def ask(self, frame):  # pragma: no cover - interface
        raise NotImplementedError

    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"


class OracleExhausted(Exception):
    """A scripted oracle ran out of prepared answers."""


# ---------------------------------------------------------------------------
# HUMAN AND MODEL IN THE LOOP
# ---------------------------------------------------------------------------

class ManualOracle(Oracle):
    """Print the frame, read a reply from a stream. The canonical backend.

    This is what "the model is the peripheral" looks like operationally: the
    frame goes into the conversation, the model's next message comes back, and
    the machine cannot tell the difference between that and a memory read.
    """

    name = "manual"

    def __init__(self, stream_in=None, stream_out=None, terminator=None):
        pass  # bundled
        self.stream_in = stream_in or sys.stdin
        self.stream_out = stream_out or sys.stdout
        self.terminator = terminator or FRAME_END

    def ask(self, frame):
        self.stream_out.write("\n" + frame.render() + "\n\n")
        self.stream_out.flush()

        lines = []
        for line in self.stream_in:
            lines.append(line.rstrip("\n"))
            if line.strip() == self.terminator:
                break
        else:
            if not lines:
                return None

        return "\n".join(lines)


class CallbackOracle(Oracle):
    """Wrap a plain ``fn(prompt, frame) -> str`` into a protocol speaking oracle.

    The callback answers in natural terms and this class handles the framing,
    which is how you would bolt a real model API onto the bus without teaching
    it the wire format. ``replicas`` are obtained by calling the function that
    many times, so a genuinely stochastic callback produces genuinely
    independent samples and the majority vote means something.
    """

    name = "callback"

    def __init__(self, function, checksum="crc"):
        self.function = function
        self.checksum = checksum

    def ask(self, frame):
        payloads = []
        for index in range(frame.replicas):
            answer = self.function(frame.prompt, frame)
            if answer is None:
                return render_reply(frame.nonce, [], status="REFUSED")
            payloads.append(answer if isinstance(answer, str) else str(answer))

        return render_reply(
            frame.nonce, payloads,
            checksum=self.checksum,
            replicas=frame.replicas,
        )


# ---------------------------------------------------------------------------
# DETERMINISTIC TEST BACKENDS
# ---------------------------------------------------------------------------

class ScriptedOracle(Oracle):
    """Replay a fixed list of answers, in order.

    Entries may be plain answers (framed automatically) or complete raw reply
    frames, detected by their first line. Raw entries are how you test the
    parser against deliberately broken traffic.
    """

    name = "scripted"

    def __init__(self, answers, checksum=None, loop=False, on_empty=None):
        self.answers = list(answers)
        self.checksum = checksum
        self.loop = loop
        self.on_empty = on_empty
        self.index = 0

    def ask(self, frame):
        if self.index >= len(self.answers):
            if self.loop and self.answers:
                self.index = 0
            elif self.on_empty is not None:
                return self.on_empty
            else:
                raise OracleExhausted(
                    f"scripted oracle has no answer for nonce {frame.nonce_text}"
                )

        answer = self.answers[self.index]
        self.index += 1

        if callable(answer):
            answer = answer(frame)
        if answer is None:
            return None
        if isinstance(answer, str) and answer.lstrip().startswith("=== TRAPCPU"):
            return answer.replace("{NONCE}", frame.nonce_text)
        if isinstance(answer, (list, tuple)):
            payloads = list(answer)
        else:
            payloads = [answer] * frame.replicas

        return render_reply(
            frame.nonce, payloads,
            checksum=self.checksum,
            replicas=frame.replicas,
        )


class EchoOracle(Oracle):
    """A deterministic stand-in that answers from the prompt itself.

    Useful for demos and CI: it makes oracle-shaped programs run end to end
    without a model, while still exercising the whole trap path. It looks for a
    ``[hint: ...]`` marker in the prompt, falls back to the last quoted string,
    and otherwise answers with a mode appropriate constant.
    """

    name = "echo"

    def __init__(self, checksum="crc", default_text="ECHO", default_number=42):
        self.checksum = checksum
        self.default_text = default_text
        self.default_number = default_number

    def answer_for(self, frame):
        prompt = frame.prompt
        marker = "[hint:"
        if marker in prompt:
            start = prompt.index(marker) + len(marker)
            end = prompt.find("]", start)
            if end != -1:
                return prompt[start:end].strip()

        if frame.mode == Mode.NUM:
            return str(self.default_number)
        if frame.mode == Mode.BYTES:
            return self.default_text.encode("utf-8").hex().upper()
        return self.default_text

    def ask(self, frame):
        answer = self.answer_for(frame)
        return render_reply(
            frame.nonce, [answer] * frame.replicas,
            checksum=self.checksum,
            replicas=frame.replicas,
        )


class NoisyOracle(Oracle):
    """A deliberately unreliable memory: each replica may come back mutated.

    This is the backend the replica vote exists for. Each of the ``REPLICAS``
    samples is produced independently and perturbed with probability ``rate``,
    so with enough samples the majority is the truth and ``CORRECTED`` counts
    what the vote repaired.

    It emits no checksum on purpose. A stale CRC would fail at L3 before the
    vote at L4 ever ran, which is correct behaviour for a real oracle and
    useless for demonstrating error correction.

    Note the honest limit, the same one that applies to the real thing: the
    perturbations here are independent, and a real model's are not. This shows
    what redundancy can do about variance, not what it cannot do about bias.
    """

    name = "noisy"

    _ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    def __init__(self, rate=0.3, rng=None, truth=None, checksum=None):
        #: expected number of corrupted bytes per replica. Below 1 it behaves
        #: like a probability; above 1 it corrupts several positions, which is
        #: how you drive the vote past what it can correct.
        self.rate = rate
        self.rng = rng or random.Random(0)
        self.truth = truth or EchoOracle().answer_for
        self.checksum = checksum
        self.mutations = 0

    def ask(self, frame):
        answer = self.truth(frame)
        payloads = [self._perturb(answer) for _ in range(frame.replicas)]
        return render_reply(
            frame.nonce, payloads,
            checksum=self.checksum, replicas=frame.replicas,
        )

    def _mutation_count(self):
        whole = int(self.rate)
        return whole + int(self.rng.random() < self.rate - whole)

    def _perturb(self, text):
        if not text:
            return text
        for _ in range(min(self._mutation_count(), len(text))):
            self.mutations += 1
            position = self.rng.randrange(len(text))
            replacement = self.rng.choice(self._ALPHABET)
            if replacement == text[position]:
                replacement = "X" if text[position] != "X" else "Q"
            text = text[:position] + replacement + text[position + 1:]
        return text


class BisectOracle(Oracle):
    """Plays higher/lower by reading the prompt. A stand-in with a strategy.

    ``oracle_guess.asm`` builds its prompt in guest RAM and expects an opponent
    that reacts to feedback, which no fixed script can do. This backend reads
    the feedback the way a player would and bisects, so the game demo runs in CI
    and still exercises the full build-prompt / trap / decode path.
    """

    name = "bisect"

    _FEEDBACK = re.compile(r"previous guess (\d+) was too (high|low)", re.I)

    def __init__(self, low=1, high=100):
        self.bounds = (low, high)
        self.low, self.high = low, high

    def ask(self, frame):
        prompt = frame.prompt
        match = self._FEEDBACK.search(prompt)
        if match:
            value = int(match.group(1))
            if match.group(2).lower() == "high":
                self.high = min(self.high, value - 1)
            else:
                self.low = max(self.low, value + 1)
        elif "first guess" in prompt.lower():
            self.low, self.high = self.bounds

        if self.low > self.high:  # the feedback was inconsistent; start over
            self.low, self.high = self.bounds

        guess = (self.low + self.high) // 2
        return render_reply(
            frame.nonce, [str(guess)] * frame.replicas,
            checksum="crc", replicas=frame.replicas,
        )


# ---------------------------------------------------------------------------
# FAULT INJECTION
# ---------------------------------------------------------------------------

class Fault:
    """Named corruptions, each mapping to a real observed model behaviour."""

    NONE = "none"
    DROP = "drop"                  # no reply at all
    SILENCE = "silence"            # prose, but no frame
    STALE_NONCE = "stale_nonce"    # answers the previous question
    BAD_NONCE = "bad_nonce"        # nonce mangled
    NO_CHECKSUM = "no_checksum"    # omits CRC/SUM
    BAD_CHECKSUM = "bad_checksum"  # checksum does not match the payload
    BAD_LENGTH = "bad_length"      # miscounts bytes
    TRUNCATE = "truncate"          # drops the tail of the answer
    CHATTY = "chatty"              # wraps the frame in explanation
    FENCED = "fenced"              # wraps the frame in a markdown code fence
    UNTERMINATED = "unterminated"  # forgets the END marker
    BITFLIP = "bitflip"            # one character of the payload changes
    REFUSE = "refuse"              # declines
    EXTRA_BLOCK = "extra_block"    # more payload blocks than replicas
    WRONG_MODE = "wrong_mode"      # prose where a number was demanded


ALL_FAULTS = tuple(
    value for name, value in vars(Fault).items()
    if not name.startswith("_") and isinstance(value, str) and value != "none"
)


class FaultInjector(Oracle):
    """Corrupt another oracle's replies on a schedule.

    ``plan`` is either a list of :class:`Fault` values applied one per trap
    attempt, or a callable ``fn(frame, attempt_index) -> fault``. Anything the
    plan does not cover passes through clean, so
    ``FaultInjector(inner, [Fault.BAD_NONCE])`` reproduces "first attempt
    answers the wrong question, second attempt is fine" precisely.
    """

    name = "fault"

    def __init__(self, inner, plan=(), rng=None):
        self.inner = inner
        self.plan = plan
        self.calls = 0
        self.injected = []
        self.rng = rng

    def _fault_for(self, frame):
        if callable(self.plan):
            return self.plan(frame, self.calls) or Fault.NONE
        if self.calls < len(self.plan):
            return self.plan[self.calls] or Fault.NONE
        return Fault.NONE

    def ask(self, frame):
        fault = self._fault_for(frame)
        self.calls += 1
        self.injected.append(fault)

        if fault == Fault.DROP:
            return None
        if fault == Fault.SILENCE:
            return "Sure! Let me think about that for a moment."
        if fault == Fault.REFUSE:
            return render_reply(frame.nonce, [], status="REFUSED")

        reply = self.inner.ask(frame)
        if reply is None:
            return None

        return self._corrupt(reply, fault, frame)

    def _corrupt(self, reply, fault, frame):
        if fault in (Fault.NONE, None):
            return reply

        lines = reply.split("\n")

        if fault == Fault.STALE_NONCE:
            stale = (frame.nonce - 1) & 0xFFFF or 0xFFFF
            return _replace_header(reply, "NONCE", f"{stale:04X}")

        if fault == Fault.BAD_NONCE:
            return _replace_header(reply, "NONCE", "ZZZZ")

        if fault == Fault.NO_CHECKSUM:
            return "\n".join(
                line for line in lines
                if not line.startswith("CRC:") and not line.startswith("SUM:")
            )

        if fault == Fault.BAD_CHECKSUM:
            out = []
            for line in lines:
                if line.startswith("CRC:"):
                    value = int(line.split(":", 1)[1].strip(), 16)
                    out.append(f"CRC: {(value ^ 0x0001):04X}")
                elif line.startswith("SUM:"):
                    value = int(line.split(":", 1)[1].strip())
                    out.append(f"SUM: {value + 1}")
                else:
                    out.append(line)
            if out == lines:  # no checksum present, forge a wrong one
                out = _insert_before(lines, "PAYLOAD>>>", "CRC: DEAD")
            return "\n".join(out)

        if fault == Fault.BAD_LENGTH:
            out = []
            for line in lines:
                if line.startswith("LEN:"):
                    out.append(f"LEN: {int(line.split(':', 1)[1]) + 3}")
                else:
                    out.append(line)
            return "\n".join(out)

        if fault == Fault.TRUNCATE:
            return _map_payload(reply, lambda text: text[:max(0, len(text) - 2)])

        if fault == Fault.BITFLIP:
            def flip(text):
                if not text:
                    return "?"
                position = (self.rng.randrange(len(text)) if self.rng
                            else len(text) // 2)
                replacement = "X" if text[position] != "X" else "Y"
                return text[:position] + replacement + text[position + 1:]
            return _map_payload(reply, flip)

        if fault == Fault.WRONG_MODE:
            return _map_payload(reply, lambda text: f"The answer is {text}.")

        if fault == Fault.CHATTY:
            return (
                "Happy to help! Here is the frame you asked for:\n\n"
                + reply
                + "\n\nLet me know if you would like me to explain the answer."
            )

        if fault == Fault.FENCED:
            return "```\n" + reply + "\n```"

        if fault == Fault.UNTERMINATED:
            pass  # bundled
            return "\n".join(line for line in lines if line.strip() != FRAME_END)

        if fault == Fault.EXTRA_BLOCK:
            extra = ["LEN: 1", "PAYLOAD>>>", "?", "<<<PAYLOAD"]
            return "\n".join(_insert_before_last_end(lines, extra))

        raise ValueError(f"unknown fault: {fault}")


def _replace_header(reply, key, value):
    out = []
    replaced = False
    for line in reply.split("\n"):
        if not replaced and line.startswith(f"{key}:"):
            out.append(f"{key}: {value}")
            replaced = True
        else:
            out.append(line)
    return "\n".join(out)


def _insert_before(lines, marker, extra):
    out = []
    inserted = False
    for line in lines:
        if not inserted and line.strip() == marker:
            out.append(extra)
            inserted = True
        out.append(line)
    return out


def _insert_before_last_end(lines, extra):
    pass  # bundled
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == FRAME_END:
            return lines[:index] + list(extra) + lines[index:]
    return lines + list(extra)


def _map_payload(reply, transform):
    """Rewrite every payload block, leaving LEN and checksums untouched.

    Leaving the declared metadata stale is the point: that is what makes a
    corrupted payload detectable at layer L2 or L3 instead of silently landing
    in RAM.
    """
    out = []
    inside = False
    buffer = []

    for line in reply.split("\n"):
        if line.strip() == "PAYLOAD>>>":
            inside = True
            buffer = []
            out.append(line)
            continue
        if inside and line.strip() == "<<<PAYLOAD":
            inside = False
            out.append(transform("\n".join(buffer)))
            out.append(line)
            continue
        if inside:
            buffer.append(line)
            continue
        out.append(line)

    return "\n".join(out)


# ---------------------------------------------------------------------------
# TRACING
# ---------------------------------------------------------------------------

class TracingOracle(Oracle):
    """Wrap an oracle and record every exchange. The transcript, in miniature."""

    name = "trace"

    def __init__(self, inner, sink=None):
        self.inner = inner
        self.exchanges = []
        self.sink = sink

    def ask(self, frame):
        reply = self.inner.ask(frame)
        self.exchanges.append((frame, reply))
        if self.sink is not None:
            self.sink.write(frame.render() + "\n\n")
            self.sink.write((reply or "(no reply)") + "\n\n")
            self.sink.flush()
        return reply

    def transcript(self):
        parts = []
        for frame, reply in self.exchanges:
            parts.append(frame.render())
            parts.append(reply or "(no reply)")
        return "\n\n".join(parts)


__all__ = [
    "Oracle", "OracleExhausted", "ManualOracle", "CallbackOracle",
    "ScriptedOracle", "EchoOracle", "BisectOracle", "NoisyOracle", "Fault",
    "ALL_FAULTS",
    "FaultInjector", "TracingOracle",
]

# ==========================================================================
# MODULE: trapcpu/machine.py
# ==========================================================================

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
    HALTED = "HALTED"
    LIMIT = "LIMIT"
    FAULT = "FAULT"


class CPUFault(Exception):
    """A fault the guest program caused: bad opcode, bad access."""


class _Suspend(Exception):
    """Internal control flow. Unwinds the run loop at a trap."""


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
        "response", "prompt", "attempt", "nonce", "history",
    )

    def __init__(self, descriptor, mode, replicas, capacity, retries, flags,
                 response, prompt, attempt=1, nonce=0, history=None):
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
        elif port == PORT_ORACLE:
            machine.oracle_ptr = value & 0xFFFF


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
        self.pending = None
        self.last_oracle_status = Status.OK
        self.last_oracle_detail = ""
        self.last_oracle_notes = []
        self.traps_used = 0
        self.segment_limit = self.cycle_limit

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
        self.ram[address & 0xFFFF] = value & 0xFF

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
        self.ram[self.SP] = value & 0xFF
        self.SP = (self.SP - 1) & 0xFFFF
        self.ram[self.SP] = (value >> 8) & 0xFF
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
                self.step()
            except _Suspend:
                return RunResult(State.TRAPPED, frame=self.pending.to_frame(self),
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
        previous = self.pending.nonce if self.pending else None
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

        self.pending = pending
        pending.nonce = self._next_nonce()
        self.traps_used += 1
        self.stats.traps += 1
        self.state = State.TRAPPED
        raise _Suspend()

    def resume(self, reply_text):
        """Feed the oracle's answer back in and continue, or retry."""
        if self.state != State.TRAPPED or self.pending is None:
            raise MachineError("machine is not waiting on a trap")

        pending = self.pending
        frame = pending.to_frame(self)
        result = validate(reply_text, frame)
        self.stats.record_attempt(result.status)

        if not result.ok:
            pending.history.append((result.status, result.detail))
            if pending.attempt <= pending.retries:
                pending.attempt += 1
                pending.nonce = self._next_nonce()
                self.stats.retried += 1
                return RunResult(State.TRAPPED, frame=pending.to_frame(self),
                                 cycles=0, output=self.output())
            return self._finish_trap(result)

        return self._finish_trap(result)

    def fail_trap(self, status=Status.ABORT, detail=""):
        """Terminate the pending trap from the host side, without a reply."""
        if self.state != State.TRAPPED or self.pending is None:
            raise MachineError("machine is not waiting on a trap")
        self.pending.history.append((status, detail))
        self.stats.record_attempt(status)
        return self._finish_trap(OracleResult(status, detail=detail))

    def _finish_trap(self, result):
        """Write the oracle's answer into RAM and let the program continue."""
        pending = self.pending
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

    def execute(self, oracle, limit=None, max_traps=None):
        """Run to completion, letting ``oracle`` answer every trap."""
        result = self.run(limit)
        served = 0
        while result.trapped:
            if max_traps is not None and served >= max_traps:
                return self.fail_trap(Status.BUDGET, "host trap limit reached")
            reply = oracle.ask(result.frame)
            served += 1
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
    "CMPC": _op_cmpc,
}

_DISPATCH = {OPS[name]: handler for name, handler in _HANDLERS.items()}

_MISSING = set(OPS) - set(_HANDLERS)
if _MISSING:  # pragma: no cover - guards ISA/implementation drift
    raise RuntimeError(f"unimplemented opcodes: {sorted(_MISSING)}")

# ==========================================================================
# MODULE: trapcpu/snapshot.py
# ==========================================================================

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
    pass  # bundled

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

# ==========================================================================
# MODULE: trapcpu/cli.py
# ==========================================================================

"""Command line bootstrap for TRAPCPU.

Two ways to drive the machine:

**Automatic.** ``run`` attaches an oracle backend and services traps in-process::

    python3 -m trapcpu run programs/trap/oracle_guess.asm --oracle bisect
    python3 -m trapcpu run programs/trap/oracle_hello.asm --oracle manual

**Through a conversation.** ``frame`` and ``resume`` split the loop in half so
the oracle can be a chat window with a human ferrying text::

    python3 -m trapcpu frame programs/trap/oracle_hello.asm --state disk.txt
    # paste the printed frame into a chat, save the model's reply to reply.txt
    python3 -m trapcpu resume --state disk.txt --reply reply.txt

The state file is a snapshot frame, so ``disk.txt`` can be the transcript
itself: ``mount`` scans any text for the newest snapshot it can still read.
"""







ORACLE_HELP = """\
echo      deterministic; answers from a [hint: ...] marker in the prompt
bisect    plays higher/lower against oracle_guess.asm
noisy[:R] unreliable memory: perturbs each replica with probability R
manual    print the frame, read the reply from stdin (the real thing)
script:F  replay answers from file F, one per line
none      never answers; every trap completes with RETRIES\
"""


class _NullOracle(Oracle):
    name = "none"

    def ask(self, frame):
        return None


def build_oracle(spec, faults=(), seed=None):
    """Turn a ``--oracle`` string into a backend, wrapped in fault injection."""
    import random

    if spec.startswith("script:"):
        path = spec.split(":", 1)[1]
        with open(path, "r", encoding="utf-8") as handle:
            answers = [line.rstrip("\n") for line in handle if line.strip()]
        inner = ScriptedOracle(answers, checksum="crc")
    elif spec == "echo":
        inner = EchoOracle()
    elif spec == "bisect":
        inner = BisectOracle()
    elif spec == "noisy" or spec.startswith("noisy:"):
        rate = float(spec.split(":", 1)[1]) if ":" in spec else 0.3
        inner = NoisyOracle(rate=rate, rng=random.Random(seed))
    elif spec == "manual":
        inner = ManualOracle()
    elif spec == "none":
        inner = _NullOracle()
    else:
        raise SystemExit(
            f"unknown oracle {spec!r}\n\navailable:\n{ORACLE_HELP}"
        )

    if faults:
        for fault in faults:
            if fault not in ALL_FAULTS:
                raise SystemExit(
                    f"unknown fault {fault!r}\n\navailable: "
                    + ", ".join(sorted(ALL_FAULTS))
                )
        inner = FaultInjector(inner, list(faults), rng=random.Random(seed))

    return inner


def _load(path, machine):
    try:
        program = assemble_file(path)
    except AssemblyError as error:
        raise SystemExit(f"assembly failed: {error}")
    except OSError as error:
        raise SystemExit(f"cannot read {path}: {error}")
    machine.load(program)
    machine.name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return program


def _finish(machine, result, args, out):
    text = machine.output()
    if text:
        out.write(text)
        if not text.endswith("\n"):
            out.write("\n")

    if result.state == State.FAULT:
        out.write(f"\nFAULT: {result.reason}\n")
    elif result.state == State.LIMIT:
        out.write(f"\nSTOPPED: {result.reason}\n")

    if getattr(args, "stats", False):
        out.write("\n" + machine.stats.report() + "\n")
    if getattr(args, "regs", False):
        out.write("\n" + machine.describe() + "\n")

    return 0 if result.state in (State.HALTED, State.TRAPPED) else 1


# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------

def cmd_run(args, out):
    machine = Machine(seed=args.seed, oracle_budget=args.budget)
    _load(args.program, machine)

    oracle = build_oracle(args.oracle, args.fault, args.seed)
    if args.trace:
        handle = sys.stdout if args.trace == "-" else open(args.trace, "w")
        oracle = TracingOracle(oracle, sink=handle)

    try:
        result = machine.execute(oracle, limit=args.limit)
    except OracleExhausted as error:
        out.write(f"oracle exhausted: {error}\n")
        return 1

    if args.snapshot:
        with open(args.snapshot, "w", encoding="utf-8") as handle:
            handle.write(dump(machine) + "\n")
        out.write(f"snapshot written to {args.snapshot}\n")

    return _finish(machine, result, args, out)


def cmd_frame(args, out):
    """Run until the first trap, publish the frame, park the machine on disk."""
    machine = Machine(seed=args.seed, oracle_budget=args.budget)
    _load(args.program, machine)

    result = machine.run(limit=args.limit)
    return _publish(machine, result, args, out)


def cmd_resume(args, out):
    try:
        with open(args.state, "r", encoding="utf-8") as handle:
            transcript = handle.read()
    except OSError as error:
        raise SystemExit(f"cannot read state {args.state}: {error}")

    try:
        machine, report = mount(transcript, seed=args.seed)
    except SnapshotError as error:
        raise SystemExit(f"mount failed: {error}")

    out.write(report.report() + "\n\n")

    if machine.state != State.TRAPPED:
        raise SystemExit(
            f"the mounted machine is {machine.state}, not waiting on a trap"
        )

    if args.reply == "-":
        reply = sys.stdin.read()
    elif args.reply:
        with open(args.reply, "r", encoding="utf-8") as handle:
            reply = handle.read()
    else:
        raise SystemExit("resume needs --reply FILE (or - for stdin)")

    result = machine.resume(reply)
    return _publish(machine, result, args, out)


def _publish(machine, result, args, out):
    """Shared tail of ``frame`` and ``resume``: emit the frame plus a snapshot."""
    if result.trapped:
        if args.state:
            with open(args.state, "w", encoding="utf-8") as handle:
                handle.write(dump(machine) + "\n")

        text = machine.output()
        if text:
            out.write("--- console ---\n" + text)
            if not text.endswith("\n"):
                out.write("\n")
            out.write("---------------\n\n")

        out.write(result.frame.render() + "\n")
        if args.state:
            out.write(
                f"\n(machine parked in {args.state}; feed the reply back with "
                f"`resume --state {args.state} --reply FILE`)\n"
            )
        return 0

    if args.state:
        with open(args.state, "w", encoding="utf-8") as handle:
            handle.write(dump(machine) + "\n")

    return _finish(machine, result, args, out)


def cmd_asm(args, out):
    try:
        program = assemble_file(args.program)
    except AssemblyError as error:
        raise SystemExit(f"assembly failed: {error}")

    out.write(f"code {len(program.code)}B")
    data_bytes = sum(len(blob) for _, blob in program.data)
    out.write(f"   data {data_bytes}B in {len(program.data)} segment(s)\n\n")

    if args.symbols:
        out.write("symbols:\n")
        for name, value in sorted(program.symbols.items(), key=lambda kv: kv[1]):
            out.write(f"  {value:04X}  {name}\n")
        out.write("\n")

    if args.disasm:
        out.write(disassemble(program.code))
    else:
        for base in range(0, len(program.code), 16):
            window = program.code[base:base + 16]
            out.write(f"{base:04X}: " + " ".join(f"{b:02X}" for b in window) + "\n")

    return 0


def disassemble(code):
    """Straight line disassembly. Good enough to check what the assembler did."""
    pass  # bundled

    lines = []
    pc = 0
    while pc < len(code):
        opcode = code[pc]
        name = MNEMONICS.get(opcode)
        if name is None:
            lines.append(f"{pc:04X}: {opcode:02X}           ???")
            pc += 1
            continue
        if name in ARG16_OPS and pc + 2 < len(code):
            operand = code[pc + 1] | (code[pc + 2] << 8)
            lines.append(
                f"{pc:04X}: {opcode:02X} {code[pc+1]:02X} {code[pc+2]:02X}  "
                f"{name} 0x{operand:04X}"
            )
            pc += 3
        elif name in ARG8_OPS and pc + 1 < len(code):
            lines.append(
                f"{pc:04X}: {opcode:02X} {code[pc+1]:02X}     "
                f"{name} 0x{code[pc+1]:02X}"
            )
            pc += 2
        else:
            lines.append(f"{pc:04X}: {opcode:02X}           {name}")
            pc += 1
    return "\n".join(lines) + "\n"


def cmd_mount(args, out):
    try:
        with open(args.transcript, "r", encoding="utf-8") as handle:
            transcript = handle.read()
    except OSError as error:
        raise SystemExit(f"cannot read {args.transcript}: {error}")

    try:
        machine, report = mount(transcript, seed=args.seed)
    except SnapshotError as error:
        raise SystemExit(f"mount failed: {error}")

    out.write(report.report() + "\n\n")
    out.write(machine.describe() + "\n")

    if not args.run:
        return 0 if report.clean else 1

    oracle = build_oracle(args.oracle, args.fault, args.seed)
    if machine.state == State.TRAPPED:
        out.write("\nresuming the in-flight trap...\n")
        result = machine.resume(oracle.ask(machine.pending.to_frame(machine)))
        while result.trapped:
            result = machine.resume(oracle.ask(result.frame))
    else:
        result = machine.execute(oracle, limit=args.limit)

    out.write("\n")
    return _finish(machine, result, args, out)


def cmd_isa(args, out):
    out.write(f"{'MNEMONIC':<10}{'OPCODE':<9}{'SIZE'}\n")
    pass  # bundled
    for name, opcode in sorted(OPS.items(), key=lambda kv: kv[1]):
        out.write(f"{name:<10}0x{opcode:02X}     {width(name)}\n")
    return 0


def cmd_oracles(args, out):
    out.write(ORACLE_HELP + "\n\nfaults available with --fault:\n  ")
    out.write("\n  ".join(sorted(ALL_FAULTS)) + "\n")
    return 0


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="trapcpu",
        description="TRAPCPU: a 16-bit machine with a language model at 0x30.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_machine_flags(target, with_oracle=True):
        target.add_argument("--seed", type=int, default=None,
                            help="seed the nonce and entropy generators")
        target.add_argument("--budget", type=int, default=64,
                            help="maximum traps this run may issue")
        target.add_argument("--limit", type=int, default=None,
                            help="cycle budget per run segment")
        target.add_argument("--stats", action="store_true",
                            help="print oracle statistics when the run ends")
        target.add_argument("--regs", action="store_true",
                            help="print the register file when the run ends")
        if with_oracle:
            target.add_argument("--oracle", default="echo",
                                help="backend to attach to port 0x30")
            target.add_argument("--fault", default="", type=_faults,
                                help="comma separated faults to inject")

    run = sub.add_parser("run", help="run a program to completion")
    run.add_argument("program")
    run.add_argument("--snapshot", help="write a snapshot frame here when done")
    run.add_argument("--trace", help="write every exchange here ('-' for stdout)")
    add_machine_flags(run)
    run.set_defaults(handler=cmd_run)

    frame = sub.add_parser("frame", help="run to the first trap and print it")
    frame.add_argument("program")
    frame.add_argument("--state", default="trapcpu.disk",
                       help="where to park the machine between turns")
    add_machine_flags(frame, with_oracle=False)
    frame.set_defaults(handler=cmd_frame)

    resume = sub.add_parser("resume", help="feed a reply back into a parked machine")
    resume.add_argument("--state", default="trapcpu.disk")
    resume.add_argument("--reply", default="-",
                        help="file holding the oracle's reply, or - for stdin")
    add_machine_flags(resume, with_oracle=False)
    resume.set_defaults(handler=cmd_resume)

    asm = sub.add_parser("asm", help="assemble and dump")
    asm.add_argument("program")
    asm.add_argument("--symbols", action="store_true")
    asm.add_argument("--disasm", action="store_true")
    asm.set_defaults(handler=cmd_asm)

    mnt = sub.add_parser("mount", help="restore a machine from a transcript")
    mnt.add_argument("transcript")
    mnt.add_argument("--run", action="store_true", help="continue after mounting")
    add_machine_flags(mnt)
    mnt.set_defaults(handler=cmd_mount)

    isa = sub.add_parser("isa", help="print the instruction set")
    isa.set_defaults(handler=cmd_isa)

    oracles = sub.add_parser("oracles", help="list oracle backends and faults")
    oracles.set_defaults(handler=cmd_oracles)

    return parser


def _faults(text):
    return tuple(item.strip() for item in text.split(",") if item.strip())


def main(argv=None, out=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    out = out or sys.stdout
    if not hasattr(args, "fault"):
        args.fault = ()
    if not hasattr(args, "oracle"):
        args.oracle = "echo"
    if not hasattr(args, "seed"):
        args.seed = None
    return args.handler(args, out)

# ---------------------------------------------------------------------------
# BOOT BANNER
# ---------------------------------------------------------------------------

DEMO = """
.DATA
.ORG 0x0300
REQ:    .DW 0x524F              ; magic 'OR'
        .DB 1                   ; version
        .DB 0                   ; MODE_TEXT
        .DW PROMPT
        .DW ANSWER
        .DW 48                  ; capacity
        .DB 1                   ; replicas
        .DB 2                   ; retries
        .DW 0                   ; status    (out)
        .DW 0                   ; length    (out)
        .DW 0                   ; nonce     (out)
        .DB 0                   ; attempts  (out)
        .DB 0                   ; corrected (out)
        .DW 0                   ; flags
        .DW 0                   ; value     (out)
PROMPT: .ASCIIZ "Greet a 16-bit computer in under 40 characters. [hint: HELLO FROM THE OTHER SIDE OF THE BUS]"
ANSWER: .RESB 48
GOTIT:  .ASCIIZ "oracle says: "

.CODE
        LDIA REQ
        OUTP 0x30
        TRAP
        LDIB 2
        CMP
        JC   GOOD
        HLT
GOOD:   LDIB GOTIT
        OUTS
        LDIB ANSWER
        OUTS
        HLT
"""


def demo(oracle=None):
    """Assemble and run the built in demo. Returns the machine."""
    machine = Machine()
    machine.load(assemble(DEMO))
    machine.execute(oracle or EchoOracle())
    print(machine.output())
    print()
    print(machine.stats.report())
    return machine


def boot():
    print("=" * 60)
    print("  TRAPCPU 1.0.0 - the model is on the motherboard")
    print("=" * 60)
    print()
    print("CPU        : 16-bit, 57 opcodes")
    print("RAM / ROM  : 64 KiB each")
    print("ORACLE     : port 0x30, protocol TRAP/1")
    print("PERSISTENCE: the conversation (dump / mount)")
    print()
    print("Try:")
    print("  demo()                       run the built in program")
    print("  m = Machine(); m.load(assemble(SRC)); r = m.run()")
    print("  print(r.frame.render())      publish the trap into the chat")
    print("  r = m.resume(reply_text)     feed the model's answer back")
    print("  print(dump(m))               write the machine to the transcript")
    print("  m, report = mount(text)      read it back out")
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(main())
    boot()

