"""Ablation: which oracle calls actually changed the answer, and did the
model beat a coin flip?

Two questions you can ask of any TRAPCPU program, exactly rather than
statistically, because everything in this machine except the trap is
deterministic.

**Criticality.** Force call *k* to a different answer, let the oracle answer
everything downstream as normal, and see whether the program's output changes.
Repeat for every call. What comes back is a map of which judgments were
load-bearing and which were ceremony. This is the record-replay counterfactual
discipline described in the LLM-agent-failure literature (see docs/ABLATION.md
for the citations); the only thing this implementation adds is exactness - the
substrate has no other source of nondeterminism to confound the intervention.

**Placebo.** Replace the oracle with uniform noise over the answers it actually
gave and run the program again. If the output barely moves, the model was not
doing the work - the surrounding algorithm was. This is an ordinary random
baseline, and it is startling how rarely LLM pipelines are made to face one.
"""

import random

from .assembler import assemble_file
from .machine import Machine
from .oracle import Oracle
from .protocol import PAYLOAD_CLOSE, PAYLOAD_OPEN, render_reply


def _payload(reply):
    """Pull the answer text back out of a rendered reply frame."""
    if not reply or PAYLOAD_OPEN not in reply:
        return None
    body = reply.split(PAYLOAD_OPEN, 1)[1]
    return body.split(PAYLOAD_CLOSE, 1)[0].strip()


class _Recorder(Oracle):
    """Pass through to the real oracle, keeping every prompt and answer."""

    name = "record"

    def __init__(self, inner):
        self.inner = inner
        self.prompts = []
        self.answers = []

    def ask(self, frame):
        reply = self.inner.ask(frame)
        self.prompts.append(frame.prompt)
        self.answers.append(_payload(reply))
        return reply


class _Intervention(Oracle):
    """Force one call to a chosen answer; everything else runs normally.

    Downstream calls go to the real oracle rather than a recording, so the
    program is free to ask different questions after the intervention - which
    is the whole point, and what makes this a counterfactual rather than a
    substitution.
    """

    name = "intervene"

    def __init__(self, inner, index, value, checksum="crc"):
        self.inner = inner
        self.index = index
        self.value = value
        self.checksum = checksum
        self.calls = 0

    def ask(self, frame):
        current, self.calls = self.calls, self.calls + 1
        if current == self.index:
            return render_reply(
                frame.nonce, [self.value] * frame.replicas,
                checksum=self.checksum, replicas=frame.replicas,
            )
        return self.inner.ask(frame)


class _Placebo(Oracle):
    """Answers uniformly at random from a fixed set. The null model."""

    name = "placebo"

    def __init__(self, values, rng, checksum="crc"):
        self.values = list(values)
        self.rng = rng
        self.checksum = checksum

    def ask(self, frame):
        answer = self.rng.choice(self.values)
        return render_reply(
            frame.nonce, [answer] * frame.replicas,
            checksum=self.checksum, replicas=frame.replicas,
        )


class Run:
    def __init__(self, output, prompts=(), answers=()):
        self.output = output
        self.prompts = list(prompts)
        self.answers = list(answers)


def _execute(path, oracle, seed=0):
    machine = Machine(seed=seed)
    machine.load(assemble_file(path))
    machine.execute(oracle)
    return machine.output()


def record(path, oracle, seed=0):
    recorder = _Recorder(oracle)
    output = _execute(path, recorder, seed)
    return Run(output, recorder.prompts, recorder.answers)


def line_distance(a, b):
    """How many lines differ, counting length mismatch as difference."""
    left, right = a.split("\n"), b.split("\n")
    width = max(len(left), len(right))
    left += [""] * (width - len(left))
    right += [""] * (width - len(right))
    return sum(1 for x, y in zip(left, right) if x != y)


def criticality(path, base, make_oracle, seed=0):
    """For each call, does forcing a different answer change the output?

    Alternatives come from the answers the oracle actually gave elsewhere in
    the run, so the intervention stays inside the observed answer space rather
    than inventing values the program was never built to receive.
    """
    universe = sorted({a for a in base.answers if a is not None})
    findings = []

    for index, answer in enumerate(base.answers):
        alternatives = [v for v in universe if v != answer]
        changed, seen = False, []
        for value in alternatives:
            output = _execute(path, _Intervention(make_oracle(), index, value),
                              seed)
            distance = line_distance(base.output, output)
            seen.append((value, distance))
            if output != base.output:
                changed = True
        findings.append({
            "index": index,
            "answer": answer,
            "prompt": base.prompts[index] if index < len(base.prompts) else "",
            "alternatives": seen,
            "critical": changed,
        })
    return findings


def placebo(path, base, make_oracle=None, trials=20, seed=0):
    """Run the program with noise in place of the oracle."""
    universe = sorted({a for a in base.answers if a is not None})
    if not universe:
        return {"trials": 0, "matches": 0, "distances": [], "universe": []}

    matches, distances = 0, []
    for trial in range(trials):
        rng = random.Random(seed * 1000 + trial)
        output = _execute(path, _Placebo(universe, rng), seed)
        if output == base.output:
            matches += 1
        distances.append(line_distance(base.output, output))

    return {
        "trials": trials,
        "matches": matches,
        "distances": distances,
        "universe": universe,
        "mean_distance": sum(distances) / len(distances),
    }


class _Prefix(Oracle):
    """Noise for the first *k* calls, the real oracle for the rest."""

    name = "prefix"

    def __init__(self, inner, k, values, rng, checksum="crc"):
        self.inner, self.k, self.values = inner, k, list(values)
        self.rng, self.checksum, self.calls = rng, checksum, 0

    def ask(self, frame):
        current, self.calls = self.calls, self.calls + 1
        if current < self.k:
            return render_reply(
                frame.nonce, [self.rng.choice(self.values)] * frame.replicas,
                checksum=self.checksum, replicas=frame.replicas,
            )
        return self.inner.ask(frame)


def noise_curve(path, base, make_oracle, trials=40, seed=0):
    """How many early calls can be noise before the answer breaks?

    Answers a question you can only ask on a deterministic substrate: given
    that the surrounding algorithm repairs some of the oracle's mistakes, how
    much of the oracle can you simply not pay for?

    The shape of the curve is a property of the *program*, not the model. An
    algorithm that revisits its own decisions - as bubble sort does on every
    pass - absorbs early errors and leaves only its final decisions exposed.
    """
    universe = sorted({a for a in base.answers if a is not None})
    calls = len(base.answers)
    if not universe or not calls:
        return []

    curve = []
    for k in range(calls + 1):
        hits = 0
        for trial in range(trials):
            rng = random.Random(seed * 10000 + k * 100 + trial)
            output = _execute(path, _Prefix(make_oracle(), k, universe, rng),
                              seed)
            hits += output == base.output
        curve.append({"k": k, "trials": trials, "matches": hits,
                      "rate": hits / trials})
    return curve


def free_prefix(curve, threshold=1.0):
    """The largest number of leading calls that can be noise at no cost."""
    best = 0
    for point in curve:
        if point["rate"] >= threshold:
            best = point["k"]
        else:
            break
    return best


def report(path, make_oracle, trials=20, seed=0, out=None, show_prompts=False):
    """Run both analyses and print a human readable summary."""
    import sys
    out = out or sys.stdout

    base = record(path, make_oracle(), seed)
    calls = len(base.answers)

    out.write(f"BASELINE\n  {calls} oracle calls, "
              f"{len(base.output)} bytes of output\n\n")

    if not calls:
        out.write("  program made no oracle calls; nothing to ablate\n")
        return {"base": base}

    findings = criticality(path, base, make_oracle, seed)
    critical = [f for f in findings if f["critical"]]

    out.write("CRITICALITY  (force one answer, let the rest run normally)\n")
    out.write(f"  load bearing : {len(critical)}/{calls}\n")
    out.write(f"  no effect    : {calls - len(critical)}/{calls}\n")
    for finding in findings:
        mark = "CHANGED" if finding["critical"] else "  --   "
        worst = max((d for _, d in finding["alternatives"]), default=0)
        out.write(f"    call {finding['index']:>3}  ans={finding['answer']!r:>5}"
                  f"  {mark}  max {worst} lines moved\n")
        if show_prompts:
            first = finding["prompt"].replace("\n", " / ")[:96]
            out.write(f"             {first}\n")

    stats = placebo(path, base, make_oracle, trials, seed)
    out.write(f"\nPLACEBO  (oracle replaced by uniform noise over "
              f"{stats['universe']}, {stats['trials']} trials)\n")
    out.write(f"  reproduced the real output : "
              f"{stats['matches']}/{stats['trials']}\n")
    out.write(f"  mean lines differing       : "
              f"{stats['mean_distance']:.1f}\n")

    curve = noise_curve(path, base, make_oracle, trials=max(trials, 20), seed=seed)
    free = free_prefix(curve)
    out.write(f"\nSELF CORRECTION  (first k calls replaced by noise, "
              f"{curve[0]['trials'] if curve else 0} trials each)\n")
    for point in curve:
        bar = "#" * round(point["rate"] * 28)
        out.write(f"    first {point['k']:>3} random  "
                  f"{point['rate']*100:3.0f}%  {bar}\n")

    out.write("\nVERDICT\n")
    if free:
        out.write(f"  The first {free} of {calls} calls can be coin flips with "
                  f"no change to\n  the output at all. The algorithm repairs "
                  f"them on later passes.\n")
    if stats["matches"] == stats["trials"]:
        out.write("  Noise reproduces the real output every time. The model\n"
                  "  contributed nothing here; the algorithm did the work.\n")
    elif stats["mean_distance"] < 1:
        out.write("  Noise lands within a line of the real output. Whatever\n"
                  "  the model is contributing, it is close to free.\n")
    else:
        out.write("  Noise does not reproduce the output. The model is doing\n"
                  "  real work in this program.\n")
    if critical and len(critical) < calls:
        out.write(f"  {calls - len(critical)} of {calls} calls changed nothing "
                  f"when flipped - that is\n  where the budget is going to "
                  f"waste.\n")

    return {"base": base, "findings": findings, "placebo": stats}
