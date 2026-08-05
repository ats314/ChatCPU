#!/usr/bin/env python3
"""Flatten the trapcpu package into one pasteable file.

The whole point of TRAPCPU is to run where the model lives, and a chat sandbox
has no package installer and no filesystem you can rely on. ``bootstrap.py`` is
the answer: one file, no imports outside the standard library, paste and go.

    python3 tools/bundle.py            # regenerate bootstrap.py
    python3 tools/bundle.py --check    # fail if bootstrap.py is stale (CI)

The bundler is deliberately dumb. It concatenates the modules in dependency
order and drops their intra-package imports, because those are the only imports
that cannot survive flattening. Anything cleverer would be a build system, and
a build system is exactly what this file exists to avoid.
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "trapcpu")
OUTPUT = os.path.join(ROOT, "bootstrap.py")

# Dependency order. Each module may only reference the ones above it.
MODULES = [
    "isa.py",
    "protocol.py",
    "assembler.py",
    "oracle.py",
    "machine.py",
    "snapshot.py",
    "cli.py",
]

# Imports of our own package: these are what flattening removes.
_RELATIVE_IMPORT = re.compile(r"^from \.[a-z_]* import .*?$(?:\n(?=\s).*?$)*",
                              re.MULTILINE)
_RELATIVE_ONELINE = re.compile(r"^from \.[a-z_]+ import [^(\n]+$", re.MULTILINE)
_RELATIVE_BLOCK = re.compile(
    r"^from \.[a-z_]+ import \([^)]*\)\n", re.MULTILINE | re.DOTALL
)
_INLINE_IMPORT = re.compile(r"^(\s+)from \.[a-z_]+ import .*$", re.MULTILINE)

HEADER = '''"""TRAPCPU {version} - single file bootstrap.

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
'''

FOOTER = '''

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
    print("  TRAPCPU {version} - the model is on the motherboard")
    print("=" * 60)
    print()
    print("CPU        : 16-bit, {opcount} opcodes")
    print("RAM / ROM  : 64 KiB each")
    print("ORACLE     : port 0x30, protocol TRAP/{protocol}")
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
'''


def read(name):
    with open(os.path.join(PACKAGE, name), "r", encoding="utf-8") as handle:
        return handle.read()


def strip_package_imports(text):
    """Remove intra-package imports, including parenthesised multi-line ones."""
    text = _RELATIVE_BLOCK.sub("", text)
    text = _RELATIVE_ONELINE.sub("", text)
    text = _INLINE_IMPORT.sub(
        lambda match: f"{match.group(1)}pass  # bundled", text
    )
    return text


def strip_stdlib_imports(text):
    """Drop stdlib imports; the bundle hoists them into a single header."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if line.startswith(("import ", "from ")) and not stripped.startswith("from ."):
            module = stripped.split()[1].split(".")[0]
            if module in {"argparse", "io", "os", "random", "re", "sys",
                          "collections"}:
                continue
        lines.append(line)
    return "\n".join(lines)


_MAIN_GUARD = re.compile(
    r"^if __name__ == [\"']__main__[\"']:.*?\n(?:(?:[ \t].*)?\n)*", re.MULTILINE
)


def strip_main_guard(text):
    """Drop each module's own ``__main__`` block; the bundle has exactly one."""
    return _MAIN_GUARD.sub("", text)


def module_body(name):
    text = read(name)
    text = strip_package_imports(text)
    text = strip_stdlib_imports(text)
    text = strip_main_guard(text)
    return text.strip("\n")


def build():
    version = _version()
    parts = [HEADER.format(version=version)]

    for name in MODULES:
        title = name[:-3].upper()
        parts.append(
            "\n\n"
            + "# " + "=" * 74 + "\n"
            + f"# MODULE: trapcpu/{name}\n"
            + "# " + "=" * 74 + "\n\n"
            + module_body(name)
        )

    parts.append(FOOTER.format(
        version=version,
        opcount=_opcount(),
        protocol=_protocol(),
    ))
    return "".join(parts) + "\n"


def _version():
    text = read("__init__.py")
    match = re.search(r'__version__ = "([^"]+)"', text)
    return match.group(1) if match else "0"


def _opcount():
    return len(re.findall(r'^\s{4}"[A-Z0-9]+": 0x', read("isa.py"), re.MULTILINE))


def _protocol():
    match = re.search(r"PROTOCOL_VERSION = (\d+)", read("protocol.py"))
    return match.group(1) if match else "1"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if bootstrap.py is out of date")
    parser.add_argument("--output", default=OUTPUT)
    args = parser.parse_args(argv)

    bundle = build()

    if args.check:
        if not os.path.exists(args.output):
            print(f"{args.output} does not exist; run tools/bundle.py")
            return 1
        with open(args.output, "r", encoding="utf-8") as handle:
            current = handle.read()
        if current != bundle:
            print(f"{args.output} is stale; run tools/bundle.py")
            return 1
        print(f"{args.output} is up to date ({len(bundle)} bytes)")
        return 0

    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(bundle)
    print(f"wrote {args.output} ({len(bundle)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
