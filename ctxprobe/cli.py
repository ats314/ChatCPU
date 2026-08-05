"""Command line: emit probes, wear transcripts down, scan what remains.

    python -m ctxprobe emit --sectors 16 --label before-compaction
    python -m ctxprobe emit --chain transcript.txt >> transcript.txt
    python -m ctxprobe scan transcript.txt
    python -m ctxprobe scan transcript.txt --json
    python -m ctxprobe rot transcript.txt --evict 0.25 --rewrite 2
    python -m ctxprobe demo --seed 7

``emit`` writes a frame to stdout — append it to whatever medium you are
measuring. ``scan`` reads a file (or stdin with ``-``) and prints the loss
report. ``rot`` applies the fault simulators, for rehearsing what a real
compaction will eventually do. ``demo`` runs the whole loop on a synthetic
transcript so the instrument can demonstrate itself.
"""

import argparse
import random
import sys

from .faults import (
    simulate_bitrot,
    simulate_eviction,
    simulate_header_loss,
    simulate_rewrite,
    simulate_truncation,
)
from .frame import emit
from .scan import find_probes, scan


def _read(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _parse_seed(text):
    if text is None:
        return None
    return int(text, 16)


def cmd_emit(args):
    prev = [token for token in (args.prev or "").split(",") if token.strip()]
    if args.chain:
        transcript = _read(args.chain)
        prev.extend(frame.probe_id for frame in find_probes(transcript)
                    if frame.probe_id)
    frame, _, probe_id = emit(
        seed=_parse_seed(args.seed),
        sectors=args.sectors,
        sector_bytes=args.sector_bytes,
        prev=prev,
        label=args.label,
    )
    print(frame)
    print("emitted probe %s (%d sectors x %dB)"
          % (probe_id, args.sectors, args.sector_bytes), file=sys.stderr)
    return 0


def cmd_scan(args):
    report = scan(_read(args.transcript),
                  expect=[token for token in (args.expect or "").split(",")
                          if token.strip()])
    if args.json:
        print(report.to_json(indent=2))
    else:
        print(report.report())
    return 0 if report.clean else 1


def cmd_rot(args):
    text = _read(args.transcript)
    rng = random.Random(args.seed)
    if args.truncate is not None:
        text = simulate_truncation(text, keep=args.truncate)
    if args.evict:
        text = simulate_eviction(text, fraction=args.evict, rng=rng)
    if args.bitrot:
        text = simulate_bitrot(text, flips=args.bitrot, rng=rng)
    if args.rewrite:
        text = simulate_rewrite(text, rewrites=args.rewrite, rng=rng)
    if args.strip_headers:
        text = simulate_header_loss(text)
    print(text)
    return 0


def cmd_demo(args):
    rng = random.Random(args.seed)
    filler = [
        "user: refactor the parser to handle the new frame format",
        "assistant: done; the tokenizer now splits on frame boundaries",
        "user: now make the tests pass again",
        "assistant: three failures fixed, one was a stale fixture",
    ]

    chunks = []
    prev = []
    for depth in range(3):
        frame, _, probe_id = emit(
            seed=rng.getrandbits(64), sectors=12,
            prev=list(prev), label="depth-%d" % depth,
        )
        prev.append(probe_id)
        chunks.append(frame)
        chunks.extend(filler * 4)
    transcript = "\n".join(chunks)

    print("--- pristine transcript: %d lines, 3 probes ---"
          % len(transcript.splitlines()))
    print(scan(transcript).summary())
    print()

    worn = simulate_truncation(transcript, keep=0.75)
    worn = simulate_eviction(worn, fraction=0.2, rng=rng)
    worn = simulate_bitrot(worn, flips=2, rng=rng)
    worn = simulate_rewrite(worn, rewrites=1, rng=rng)

    print("--- after truncation + eviction + bitrot + a CRC-consistent "
          "rewrite ---")
    print(scan(worn).report())
    return 0


def cmd_study(args):
    from .study import DEFAULT_ADAPTERS, render_table, run_study, to_csv, to_json

    records = run_study(
        adapters=args.adapters or DEFAULT_ADAPTERS,
        trials=args.trials,
        probes=args.probes,
        sectors=args.sectors,
        seed=args.seed,
        log=(None if args.json else
             lambda line: print("  " + line, file=sys.stderr)),
    )
    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as handle:
            handle.write(to_csv(records) + "\n")
        print("wrote %s" % args.csv, file=sys.stderr)
    if args.json:
        print(to_json(records, indent=2))
    else:
        print("CTXPROBE STUDY seed=%d trials=%d probes=%dx%d sectors "
              "(mean %% of sentinel bytes intact, by context depth)"
              % (args.seed, args.trials, args.probes, args.sectors))
        print(render_table(records, args.probes))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ctxprobe",
        description="badblocks for transcripts: measure context-window loss",
    )
    commands = parser.add_subparsers(dest="command", metavar="command")

    emit_cmd = commands.add_parser(
        "emit", help="render a sentinel probe frame to stdout")
    emit_cmd.add_argument("--sectors", type=int, default=16,
                          help="sector count (default 16)")
    emit_cmd.add_argument("--sector-bytes", type=int, default=32,
                          help="bytes per sector (default 32)")
    emit_cmd.add_argument("--seed", default=None,
                          help="64-bit hex seed (default: random)")
    emit_cmd.add_argument("--label", default=None,
                          help="freeform label recorded in the frame")
    emit_cmd.add_argument("--prev", default=None,
                          help="comma-separated ids of earlier probes")
    emit_cmd.add_argument("--chain", default=None, metavar="FILE",
                          help="scan FILE and chain to every probe found in it")
    emit_cmd.set_defaults(func=cmd_emit)

    scan_cmd = commands.add_parser(
        "scan", help="scan a transcript and report sector-level loss")
    scan_cmd.add_argument("transcript", help="file to scan, or - for stdin")
    scan_cmd.add_argument("--json", action="store_true",
                          help="machine-readable report")
    scan_cmd.add_argument("--expect", default=None,
                          help="comma-separated probe ids that must be present")
    scan_cmd.set_defaults(func=cmd_scan)

    rot_cmd = commands.add_parser(
        "rot", help="apply wear simulators to a transcript")
    rot_cmd.add_argument("transcript", help="file to degrade, or - for stdin")
    rot_cmd.add_argument("--evict", type=float, default=0.0,
                         help="fraction of sector lines to delete")
    rot_cmd.add_argument("--bitrot", type=int, default=0,
                         help="hex digits to mangle in place")
    rot_cmd.add_argument("--rewrite", type=int, default=0,
                         help="sectors to alter with a recomputed CRC")
    rot_cmd.add_argument("--truncate", type=float, default=None,
                         metavar="KEEP",
                         help="keep only the newest KEEP fraction of lines")
    rot_cmd.add_argument("--strip-headers", action="store_true",
                         help="drop MAP/SECTORS lines (summarization)")
    rot_cmd.add_argument("--seed", type=int, default=0,
                         help="fault RNG seed (default 0)")
    rot_cmd.set_defaults(func=cmd_rot)

    study_cmd = commands.add_parser(
        "study", help="measure survival curves across compaction adapters")
    study_cmd.add_argument(
        "--adapters", default=None,
        help="comma-separated adapter specs: identity, truncate:F, dedup, "
             "reflow, wear, cmd:SHELL, claude[:MODEL] "
             "(default: identity,truncate:0.5,dedup,reflow,wear)")
    study_cmd.add_argument("--trials", type=int, default=3,
                           help="transcripts per adapter (default 3)")
    study_cmd.add_argument("--probes", type=int, default=4,
                           help="probes per transcript (default 4)")
    study_cmd.add_argument("--sectors", type=int, default=12,
                           help="sectors per probe (default 12)")
    study_cmd.add_argument("--seed", type=int, default=0,
                           help="study seed (default 0)")
    study_cmd.add_argument("--json", action="store_true",
                           help="emit raw records as JSON")
    study_cmd.add_argument("--csv", default=None, metavar="FILE",
                           help="also write per-record CSV to FILE")
    study_cmd.set_defaults(func=cmd_study)

    demo_cmd = commands.add_parser(
        "demo", help="run the full emit/wear/scan loop on synthetic text")
    demo_cmd.add_argument("--seed", type=int, default=0,
                          help="demo RNG seed (default 0)")
    demo_cmd.set_defaults(func=cmd_demo)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)
