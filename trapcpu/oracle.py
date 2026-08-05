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

import random
import re
import sys

from .protocol import Mode, render_reply


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
        from .protocol import FRAME_END
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


class JudgeOracle(Oracle):
    """Answers "which of these two is worse?" so oracle_sort.asm runs offline.

    ``oracle_sort.asm`` uses the coprocessor as its comparison operator, which
    means the demo needs an opponent that holds a consistent opinion across
    every pair the sort happens to present - a fixed script cannot do that,
    because which comparisons get made depends on the answers to earlier ones.

    This backend has exactly one hardcoded opinion, listed below. That is the
    honest description of it: it is a stand-in that makes the program runnable
    with no API key and keeps CI deterministic. It is not a judge. Run with
    ``--oracle claude`` for an ordering that is actually decided rather than
    looked up.
    """

    name = "judge"

    # Severity ranks for the items oracle_sort.asm ships with. Higher is worse.
    _OPINION = {
        "a paper cut": 1,
        "a wasp sting": 2,
        "a car crash": 3,
        "a house fire": 4,
        "a hurricane": 5,
    }

    _PAIR = re.compile(r"^\s*1\)\s*(.+?)\s*^\s*2\)\s*(.+?)\s*\Z",
                       re.M | re.S)

    def rank(self, item):
        key = item.strip().lower()
        if key in self._OPINION:
            return self._OPINION[key]
        # Unknown item: fall back to something stable so the sort still
        # terminates with a total order, rather than looking clever about it.
        return 100 + sum(bytearray(key.encode("utf-8"))) % 100

    def ask(self, frame):
        match = self._PAIR.search(frame.prompt)
        if match is None:
            answer = "1"
        else:
            left, right = match.group(1), match.group(2)
            answer = "1" if self.rank(left) > self.rank(right) else "2"
        return render_reply(
            frame.nonce, [answer] * frame.replicas,
            checksum="crc", replicas=frame.replicas,
        )


class NavigatorOracle(Oracle):
    """Steers toward a target by reading coordinates out of the prompt.

    The counterpart of :class:`BisectOracle` for the async pilot demo: the
    guest publishes "ship at (x,y) ... target at (tx,ty)" and keeps flying
    while this backend decides. Directions are the pilot program's encoding:
    1 up, 2 down, 3 left, 4 right.
    """

    name = "navigator"

    _COORDS = re.compile(
        r"ship at \((\d+),(\d+)\).*?target at \((\d+),(\d+)\)", re.I | re.S
    )

    def ask(self, frame):
        match = self._COORDS.search(frame.prompt)
        if not match:
            return render_reply(frame.nonce, [], status="REFUSED")
        x, y, tx, ty = (int(match.group(i)) for i in range(1, 5))

        # Close the larger gap first; ties break horizontal.
        if abs(tx - x) >= abs(ty - y) and tx != x:
            direction = 4 if tx > x else 3
        elif ty != y:
            direction = 2 if ty > y else 1
        else:
            direction = 4  # already there; the guest will notice before we do

        return render_reply(
            frame.nonce, [str(direction)] * frame.replicas,
            checksum="crc", replicas=frame.replicas,
        )


class MuxOracle(Oracle):
    """Route each frame to a backend by its DEVICE number.

    Ports 0x30-0x37 select devices 0-7, so a guest can put a fast cheap
    model on one port and a strong slow one on another and choose per
    question - big.LITTLE for intelligence. Frames for devices with no
    backend attached get no reply, which completes the trap with RETRIES:
    an empty slot on the bus reads as a device that never answers.
    """

    name = "mux"

    def __init__(self, devices):
        self.devices = dict(devices)

    def ask(self, frame):
        backend = self.devices.get(getattr(frame, "device", 0))
        if backend is None:
            return None
        return backend.ask(frame)


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
            from .protocol import FRAME_END
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
    from .protocol import FRAME_END
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
    "ScriptedOracle", "EchoOracle", "BisectOracle", "NoisyOracle",
    "NavigatorOracle", "MuxOracle", "Fault", "ALL_FAULTS",
    "FaultInjector", "TracingOracle",
]
