import io
import random
import unittest

from trapcpu.oracle import (
    ALL_FAULTS,
    BisectOracle,
    CallbackOracle,
    EchoOracle,
    Fault,
    FaultInjector,
    ManualOracle,
    NoisyOracle,
    OracleExhausted,
    ScriptedOracle,
    TracingOracle,
)
from trapcpu.protocol import FRAME_END, Mode, Status, TrapFrame, validate


def frame(**kwargs):
    kwargs.setdefault("nonce", 0x1234)
    kwargs.setdefault("prompt", "Name a colour. [hint: BLUE]")
    kwargs.setdefault("capacity", 64)
    return TrapFrame(**kwargs)


class TestEchoOracle(unittest.TestCase):

    def test_hint_marker_is_honoured(self):
        request = frame()
        self.assertTrue(validate(EchoOracle().ask(request), request).ok)
        self.assertEqual(
            validate(EchoOracle().ask(request), request).payload, b"BLUE"
        )

    def test_num_mode_gets_a_number(self):
        request = frame(mode=Mode.NUM, prompt="how many?")
        result = validate(EchoOracle().ask(request), request)
        self.assertEqual(result.value, 42)

    def test_bytes_mode_gets_hex(self):
        request = frame(mode=Mode.BYTES, prompt="give bytes")
        self.assertTrue(validate(EchoOracle().ask(request), request).ok)

    def test_replicas_are_all_answered(self):
        request = frame(replicas=4)
        self.assertTrue(validate(EchoOracle().ask(request), request).ok)


class TestScriptedOracle(unittest.TestCase):

    def test_answers_are_replayed_in_order(self):
        oracle = ScriptedOracle(["RED", "GREEN"], checksum="crc")
        self.assertEqual(validate(oracle.ask(frame()), frame()).payload, b"RED")
        self.assertEqual(validate(oracle.ask(frame()), frame()).payload, b"GREEN")

    def test_running_out_raises(self):
        oracle = ScriptedOracle(["RED"])
        oracle.ask(frame())
        with self.assertRaises(OracleExhausted):
            oracle.ask(frame())

    def test_looping(self):
        oracle = ScriptedOracle(["RED"], loop=True)
        oracle.ask(frame())
        self.assertIsNotNone(oracle.ask(frame()))

    def test_raw_frames_pass_through_with_nonce_substitution(self):
        raw = (
            "=== TRAPCPU/1 ORACLE REPLY ===\nNONCE: {NONCE}\nSTATUS: OK\n"
            "LEN: 3\nPAYLOAD>>>\nRED\n<<<PAYLOAD\n" + FRAME_END
        )
        oracle = ScriptedOracle([raw])
        self.assertTrue(validate(oracle.ask(frame()), frame()).ok)

    def test_callables_receive_the_frame(self):
        oracle = ScriptedOracle([lambda f: f"{f.nonce:04X}"])
        result = validate(oracle.ask(frame()), frame())
        self.assertEqual(result.payload, b"1234")

    def test_a_list_entry_supplies_per_replica_answers(self):
        oracle = ScriptedOracle([["A", "B", "A"]])
        result = validate(oracle.ask(frame(replicas=3)), frame(replicas=3))
        self.assertEqual(result.payload, b"A")
        self.assertEqual(result.corrected, 1)


class TestCallbackOracle(unittest.TestCase):

    def test_a_plain_function_becomes_a_peripheral(self):
        oracle = CallbackOracle(lambda prompt, f: prompt.upper()[:4])
        result = validate(oracle.ask(frame(prompt="teal")), frame(prompt="teal"))
        self.assertEqual(result.payload, b"TEAL")

    def test_replicas_call_the_function_repeatedly(self):
        calls = []

        def answer(prompt, request):
            calls.append(prompt)
            return "X" * (1 + len(calls) % 2)

        oracle = CallbackOracle(answer)
        oracle.ask(frame(replicas=3))
        self.assertEqual(len(calls), 3)

    def test_none_becomes_a_refusal(self):
        oracle = CallbackOracle(lambda prompt, f: None)
        self.assertEqual(
            validate(oracle.ask(frame()), frame()).status, Status.REFUSED
        )


class TestManualOracle(unittest.TestCase):

    def test_it_prints_the_frame_and_reads_until_the_end_marker(self):
        reply = (
            "=== TRAPCPU/1 ORACLE REPLY ===\nNONCE: 1234\nSTATUS: OK\n"
            "LEN: 4\nPAYLOAD>>>\nBLUE\n<<<PAYLOAD\n" + FRAME_END + "\n"
            "trailing chatter that must not be read\n"
        )
        out = io.StringIO()
        oracle = ManualOracle(stream_in=io.StringIO(reply), stream_out=out)
        text = oracle.ask(frame())
        self.assertIn("ORACLE REQUEST", out.getvalue())
        self.assertNotIn("trailing chatter", text)
        self.assertTrue(validate(text, frame()).ok)

    def test_empty_input_means_no_answer(self):
        oracle = ManualOracle(stream_in=io.StringIO(""), stream_out=io.StringIO())
        self.assertIsNone(oracle.ask(frame()))


class TestNoisyOracle(unittest.TestCase):
    """The unreliable memory the replica vote exists to correct."""

    def _run(self, rate, seed, replicas=5):
        request = frame(replicas=replicas, prompt="say CORAL [hint: CORAL]")
        oracle = NoisyOracle(rate=rate, rng=random.Random(seed))
        return validate(oracle.ask(request), request)

    def test_zero_noise_is_unanimous(self):
        result = self._run(0.0, 1)
        self.assertEqual(result.status, Status.DEGRADED)  # no checksum by design
        self.assertEqual(result.payload, b"CORAL")
        self.assertEqual(result.corrected, 0)

    def test_light_noise_is_usually_repaired_and_never_silently_wrong(self):
        """The load-bearing property: a successful vote is a correct vote.

        Some runs still fail even at light noise, when two replicas happen to
        corrupt the same byte position differently and no majority exists. That
        is the design working, not a flake: it fails loudly as NO_QUORUM rather
        than electing a plurality.
        """
        succeeded = repaired = 0
        for seed in range(24):
            result = self._run(0.4, seed)
            if result.ok:
                succeeded += 1
                self.assertEqual(result.payload, b"CORAL", f"seed {seed}")
                repaired += result.corrected
            else:
                self.assertEqual(result.status, Status.NO_QUORUM, f"seed {seed}")

        self.assertGreater(succeeded, 12, "light noise should mostly recover")
        self.assertGreater(repaired, 0, "the noise never actually fired")

    def test_heavy_noise_exceeds_what_the_vote_can_correct(self):
        outcomes = [self._run(3.0, seed) for seed in range(12)]
        failures = [result for result in outcomes if not result.ok]
        self.assertGreater(len(failures), 0)
        for result in failures:
            self.assertEqual(result.status, Status.NO_QUORUM)
        for result in outcomes:
            if result.ok:
                self.assertEqual(result.payload, b"CORAL")

    def test_it_emits_no_checksum_so_the_vote_is_reached(self):
        """A stale CRC would fail at L3 before L4 ever ran."""
        request = frame(replicas=3, prompt="say CORAL [hint: CORAL]")
        text = NoisyOracle(rate=1.0, rng=random.Random(1)).ask(request)
        self.assertNotIn("CRC:", text)
        self.assertNotIn("SUM:", text)

    def test_one_replica_cannot_be_corrected_at_all(self):
        result = self._run(3.0, 1, replicas=1)
        self.assertEqual(result.corrected, 0)
        self.assertNotEqual(result.payload, b"CORAL")


class TestBisectOracle(unittest.TestCase):

    def test_first_guess_is_the_midpoint(self):
        oracle = BisectOracle()
        request = frame(mode=Mode.NUM, prompt="This is your first guess.")
        self.assertEqual(validate(oracle.ask(request), request).value, 50)

    def test_it_narrows_on_feedback(self):
        oracle = BisectOracle()
        low = frame(mode=Mode.NUM, prompt="Your previous guess 50 was too high.")
        self.assertEqual(validate(oracle.ask(low), low).value, 25)

    def test_inconsistent_feedback_restarts_rather_than_hanging(self):
        oracle = BisectOracle()
        high = frame(mode=Mode.NUM, prompt="Your previous guess 100 was too low.")
        request = frame(mode=Mode.NUM, prompt="Your previous guess 1 was too high.")
        oracle.ask(high)
        self.assertIsNotNone(oracle.ask(request))


class TestFaultInjection(unittest.TestCase):

    EXPECTED = {
        Fault.SILENCE: Status.NO_FRAME,
        Fault.STALE_NONCE: Status.BAD_NONCE,
        Fault.BAD_NONCE: Status.BAD_NONCE,
        Fault.NO_CHECKSUM: Status.DEGRADED,
        Fault.BAD_CHECKSUM: Status.BAD_CHECKSUM,
        Fault.BAD_LENGTH: Status.BAD_LENGTH,
        Fault.TRUNCATE: Status.BAD_LENGTH,
        Fault.BITFLIP: Status.BAD_CHECKSUM,
        Fault.REFUSE: Status.REFUSED,
        Fault.UNTERMINATED: Status.MALFORMED,
        Fault.EXTRA_BLOCK: Status.MALFORMED,
        Fault.CHATTY: Status.OK,
        Fault.FENCED: Status.OK,
        Fault.WRONG_MODE: Status.BAD_LENGTH,
    }

    def test_every_fault_produces_the_status_it_should(self):
        for fault, expected in self.EXPECTED.items():
            oracle = FaultInjector(EchoOracle(), [fault])
            reply = oracle.ask(frame())
            self.assertIsNotNone(reply, fault)
            self.assertEqual(validate(reply, frame()).status, expected, fault)

    def test_drop_returns_nothing(self):
        oracle = FaultInjector(EchoOracle(), [Fault.DROP])
        self.assertIsNone(oracle.ask(frame()))

    def test_every_named_fault_is_covered_by_this_test(self):
        covered = set(self.EXPECTED) | {Fault.DROP}
        self.assertEqual(covered, set(ALL_FAULTS))

    def test_wrong_mode_is_a_decode_failure_when_a_number_was_demanded(self):
        request = frame(mode=Mode.NUM, prompt="how many? [hint: 12]")
        oracle = FaultInjector(EchoOracle(checksum=None), [Fault.WRONG_MODE])
        reply = oracle.ask(request)
        # LEN is left stale by the injector, so L2 catches it before L5 can.
        self.assertIn(
            validate(reply, request).status,
            (Status.BAD_LENGTH, Status.BAD_ENCODING),
        )

    def test_the_plan_only_covers_the_attempts_it_names(self):
        oracle = FaultInjector(EchoOracle(), [Fault.BAD_NONCE])
        self.assertEqual(validate(oracle.ask(frame()), frame()).status,
                         Status.BAD_NONCE)
        self.assertEqual(validate(oracle.ask(frame()), frame()).status, Status.OK)

    def test_a_callable_plan_can_look_at_the_frame(self):
        oracle = FaultInjector(
            EchoOracle(),
            lambda request, index: Fault.REFUSE if index == 1 else Fault.NONE,
        )
        self.assertTrue(validate(oracle.ask(frame()), frame()).ok)
        self.assertEqual(validate(oracle.ask(frame()), frame()).status,
                         Status.REFUSED)
        self.assertEqual(oracle.injected, [Fault.NONE, Fault.REFUSE])


class TestTracingOracle(unittest.TestCase):

    def test_exchanges_are_recorded(self):
        sink = io.StringIO()
        oracle = TracingOracle(EchoOracle(), sink=sink)
        oracle.ask(frame())
        self.assertEqual(len(oracle.exchanges), 1)
        self.assertIn("ORACLE REQUEST", sink.getvalue())
        self.assertIn("ORACLE REPLY", oracle.transcript())


if __name__ == "__main__":
    unittest.main()
