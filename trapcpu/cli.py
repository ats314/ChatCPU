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

import argparse
import sys

from .assembler import AssemblyError, assemble_file
from .llm import ClaudeOracle, OracleTransportError
from .isa import MNEMONICS, OPS
from .machine import Machine, State
from .oracle import (
    ALL_FAULTS,
    BisectOracle,
    EchoOracle,
    FaultInjector,
    ManualOracle,
    MuxOracle,
    NavigatorOracle,
    NoisyOracle,
    Oracle,
    OracleExhausted,
    ScriptedOracle,
    TracingOracle,
)
from .snapshot import SnapshotError, dump, mount

ORACLE_HELP = """\
echo      deterministic; answers from a [hint: ...] marker in the prompt
bisect    plays higher/lower against oracle_guess.asm
navigator steers the async pilot demo toward its target
noisy[:R] unreliable memory: perturbs each replica with probability R
manual    print the frame, read the reply from stdin (the real thing)
claude[:MODEL]  a real Claude model over the Anthropic API (needs
          ANTHROPIC_API_KEY; default model claude-opus-5)
script:F  replay answers from file F, one per line
none      never answers; every trap completes with RETRIES
A,B,...   comma list: device 0 gets A, device 1 gets B (ports 0x30, 0x31...)\
"""


class _NullOracle(Oracle):
    name = "none"

    def ask(self, frame):
        return None


def build_oracle(spec, faults=(), seed=None):
    """Turn a ``--oracle`` string into a backend, wrapped in fault injection."""
    import random

    if "," in spec:
        parts = [item.strip() for item in spec.split(",") if item.strip()]
        return MuxOracle({
            device: build_oracle(part, faults, seed)
            for device, part in enumerate(parts)
        })

    if spec.startswith("script:"):
        path = spec.split(":", 1)[1]
        with open(path, "r", encoding="utf-8") as handle:
            answers = [line.rstrip("\n") for line in handle if line.strip()]
        inner = ScriptedOracle(answers, checksum="crc")
    elif spec == "echo":
        inner = EchoOracle()
    elif spec == "bisect":
        inner = BisectOracle()
    elif spec == "navigator":
        inner = NavigatorOracle()
    elif spec == "noisy" or spec.startswith("noisy:"):
        rate = float(spec.split(":", 1)[1]) if ":" in spec else 0.3
        inner = NoisyOracle(rate=rate, rng=random.Random(seed))
    elif spec == "manual":
        inner = ManualOracle()
    elif spec == "claude" or spec.startswith("claude:"):
        model = spec.split(":", 1)[1] if ":" in spec else None
        try:
            inner = ClaudeOracle(**({"model": model} if model else {}))
        except OracleTransportError as error:
            raise SystemExit(str(error))
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
        result = machine.execute(oracle, limit=args.limit,
                                 async_latency=args.latency)
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
    from .isa import ARG8_OPS, ARG16_OPS

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
    from .isa import width
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
    run.add_argument("--latency", type=int, default=0,
                     help="cycles the machine keeps running while an async "
                          "(TRAPA) request is being answered")
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
