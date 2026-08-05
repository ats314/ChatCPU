"""The study rig: measure what real compactors do to salted transcripts.

This is the experiment the instrument exists for. Build synthetic
conversations with probes salted at intervals, run them through *adapters* —
anything that maps transcript text to smaller transcript text — and scan what
comes back. Because probes carry their position, the result is not a loss
percentage but a survival curve over context depth, per adapter, comparable
across adapters.

Adapters model the compaction strategies real harnesses use:

======================  =====================================================
``identity``            control: no compaction. Must score 100%.
``truncate:F``          keep the newest F fraction — plain context eviction.
``dedup``               drop repeated lines — "smart" dedup compaction.
``reflow``              re-wrap long lines at 72 columns — what naive text
                        processing and copy-paste do to a transcript.
``wear``                the fault simulators combined — synthetic reference.
``cmd:SHELL``           any external compactor: text on stdin, text on stdout.
``claude[:MODEL]``      a real model summarizes the transcript — the actual
                        experiment. Standard library HTTPS, same premise as
                        trapcpu's oracle backend. Needs ANTHROPIC_API_KEY.
======================  =====================================================

Every trial is deterministic given the seed (string-seeded ``random.Random``,
stable across Python versions), so a study is a reproducible experiment, not
an anecdote. ``run_study`` returns structured records; the CLI renders the
table, ``--json`` and ``--csv`` feed charts.
"""

import json
import random
import subprocess
import textwrap

from .frame import emit
from .faults import (
    simulate_bitrot,
    simulate_eviction,
    simulate_rewrite,
    simulate_truncation,
)
from .scan import Status, scan

FILLER_TOPICS = [
    "the parser rewrite", "flaky test triage", "the release checklist",
    "cache invalidation", "the migration script", "renaming the module",
    "the deploy pipeline", "error budgets", "the onboarding doc",
]

FILLER_SHAPES = [
    "user: can you look at {topic} next",
    "assistant: done — {topic} is handled, two files changed",
    "user: what's the status of {topic}?",
    "assistant: {topic} is blocked on review, everything else is green",
    "assistant: I checked {topic}; no action needed",
]

# A recurring line, verbatim, so dedup-style compaction has something to eat.
BOILERPLATE = "assistant: Let me know if you need anything else!"


def build_transcript(rng, probes=4, sectors=12, filler_lines=30):
    """A synthetic conversation with probes salted at intervals.

    Returns ``(text, probe_ids)`` with ids in emission order — index 0 is the
    oldest (deepest) probe, the one truncation eats first.
    """
    chunks = []
    ids = []
    for depth in range(probes):
        frame, _, probe_id = emit(
            seed=rng.getrandbits(64),
            sectors=sectors,
            prev=list(ids),
            label="depth-%d" % depth,
        )
        ids.append(probe_id)
        chunks.append(frame)
        for _ in range(filler_lines):
            if rng.random() < 0.2:
                chunks.append(BOILERPLATE)
            else:
                shape = rng.choice(FILLER_SHAPES)
                chunks.append(shape.format(topic=rng.choice(FILLER_TOPICS)))
    return "\n".join(chunks), ids


# ---------------------------------------------------------------------------
# ADAPTERS
# ---------------------------------------------------------------------------

def _adapter_identity(text, rng):
    return text


def _adapter_truncate(keep):
    def run(text, rng):
        return simulate_truncation(text, keep=keep)
    return run


def _adapter_dedup(text, rng):
    seen = set()
    out = []
    for line in text.split("\n"):
        if line.strip() and line in seen:
            continue
        seen.add(line)
        out.append(line)
    return "\n".join(out)


def _adapter_reflow(text, rng):
    out = []
    for line in text.split("\n"):
        if len(line) > 72:
            out.extend(textwrap.wrap(line, width=72))
        else:
            out.append(line)
    return "\n".join(out)


def _adapter_wear(text, rng):
    text = simulate_eviction(text, fraction=0.15, rng=rng)
    text = simulate_bitrot(text, flips=2, rng=rng)
    return simulate_rewrite(text, rewrites=1, rng=rng)


def _adapter_cmd(command):
    def run(text, rng):
        result = subprocess.run(
            command, shell=True, input=text, capture_output=True,
            text=True, timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "adapter command failed (%d): %s"
                % (result.returncode, result.stderr.strip()[:200]))
        return result.stdout
    return run


def _adapter_claude(model=None):
    from .llmadapter import compact_with_claude

    def run(text, rng):
        return compact_with_claude(text, model=model)
    return run


def resolve_adapter(spec):
    """Turn an adapter spec string into ``(name, callable)``."""
    if spec == "identity":
        return spec, _adapter_identity
    if spec.startswith("truncate:"):
        return spec, _adapter_truncate(float(spec.split(":", 1)[1]))
    if spec == "dedup":
        return spec, _adapter_dedup
    if spec == "reflow":
        return spec, _adapter_reflow
    if spec == "wear":
        return spec, _adapter_wear
    if spec.startswith("cmd:"):
        return spec, _adapter_cmd(spec.split(":", 1)[1])
    if spec == "claude" or spec.startswith("claude:"):
        _, _, model = spec.partition(":")
        return spec, _adapter_claude(model or None)
    raise ValueError("unknown adapter: %r" % spec)


DEFAULT_ADAPTERS = "identity,truncate:0.5,dedup,reflow,wear"


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------

def run_study(adapters=DEFAULT_ADAPTERS, trials=3, probes=4, sectors=12,
              seed=0, log=None):
    """Run every adapter over ``trials`` fresh transcripts. Returns records.

    One record per (adapter, trial, probe position)::

        {"adapter": ..., "trial": ..., "position": 0, "probe_id": ...,
         "vanished": False, "bytes_intact": ..., "bytes_lost": ...,
         "survival": 0.87, "counts": {status: n, ...}}
    """
    specs = [resolve_adapter(token.strip())
             for token in adapters.split(",") if token.strip()]
    records = []

    for name, adapter in specs:
        for trial in range(trials):
            rng = random.Random("ctxprobe-study:%s:%s:%d"
                                % (seed, name, trial))
            transcript, ids = build_transcript(
                rng, probes=probes, sectors=sectors)
            survived = adapter(transcript, rng)
            report = scan(survived, expect=ids)

            by_id = {}
            for probe in report.probes:
                by_id.setdefault(probe.probe_id, probe)

            for position, probe_id in enumerate(ids):
                probe = by_id.get(probe_id)
                if probe is None:
                    records.append({
                        "adapter": name, "trial": trial,
                        "position": position, "probe_id": probe_id,
                        "vanished": True, "bytes_intact": 0,
                        "bytes_lost": 0, "survival": 0.0,
                        "counts": dict.fromkeys(Status.ALL, 0),
                    })
                    continue
                total = probe.bytes_intact + probe.bytes_lost
                records.append({
                    "adapter": name, "trial": trial,
                    "position": position, "probe_id": probe_id,
                    "vanished": False,
                    "bytes_intact": probe.bytes_intact,
                    "bytes_lost": probe.bytes_lost,
                    "survival": probe.bytes_intact / total if total else 0.0,
                    "counts": probe.counts,
                })
            if log:
                log("%s trial %d: %s" % (name, trial, report.summary()))

    return records


# ---------------------------------------------------------------------------
# RENDER
# ---------------------------------------------------------------------------

def render_table(records, probes):
    """The survival curve as text: one row per adapter, one column per depth."""
    adapters = []
    for record in records:
        if record["adapter"] not in adapters:
            adapters.append(record["adapter"])

    width = max([len(name) for name in adapters] + [7])
    header = "adapter".ljust(width)
    for position in range(probes):
        tag = "p%d" % position
        if position == 0:
            tag += "(old)"
        elif position == probes - 1:
            tag += "(new)"
        header += tag.rjust(10)
    header += "  vanished"
    lines = [header]

    for name in adapters:
        mine = [r for r in records if r["adapter"] == name]
        row = name.ljust(width)
        for position in range(probes):
            cell = [r for r in mine if r["position"] == position]
            survival = (sum(r["survival"] for r in cell) / len(cell)
                        if cell else 0.0)
            row += ("%d%%" % round(100 * survival)).rjust(10)
        gone = sum(1 for r in mine if r["vanished"])
        row += ("  %d/%d" % (gone, len(mine)))
        lines.append(row)

    return "\n".join(lines)


def to_csv(records):
    fields = ["adapter", "trial", "position", "probe_id", "vanished",
              "bytes_intact", "bytes_lost", "survival"]
    fields += ["n_%s" % status.lower() for status in Status.ALL]
    lines = [",".join(fields)]
    for record in records:
        row = [str(record[field]) for field in
               ("adapter", "trial", "position", "probe_id", "vanished",
                "bytes_intact", "bytes_lost")]
        row.append("%.4f" % record["survival"])
        row.extend(str(record["counts"][status]) for status in Status.ALL)
        lines.append(",".join(row))
    return "\n".join(lines)


def to_json(records, indent=None):
    return json.dumps(records, indent=indent)
