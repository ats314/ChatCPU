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

import os

from .isa import ARG8_OPS, ARG16_OPS, OPS

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
