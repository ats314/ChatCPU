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

import re
from collections import Counter

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
