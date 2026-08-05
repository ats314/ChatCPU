"""Ablation: criticality, placebo, and the self-correction curve."""

import io
import unittest

from trapcpu import ablate
from trapcpu.cli import build_oracle

SORT = "programs/trap/oracle_sort.asm"
HELLO = "programs/trap/oracle_hello.asm"


def judge():
    return build_oracle("judge")


class TestRecording(unittest.TestCase):

    def test_it_records_one_answer_per_call(self):
        run = ablate.record(SORT, judge())
        self.assertEqual(len(run.answers), 16)
        self.assertEqual(len(run.prompts), 16)
        self.assertTrue(all(a in ("1", "2") for a in run.answers))

    def test_recording_does_not_disturb_the_program(self):
        self.assertEqual(ablate.record(SORT, judge()).output,
                         ablate._execute(SORT, judge()))


class TestCriticality(unittest.TestCase):

    def test_only_the_final_pass_is_load_bearing(self):
        """The finding, pinned.

        Bubble sort makes four passes of four. Errors in the first three get
        repaired by a later pass; the fourth has nothing after it.
        """
        base = ablate.record(SORT, judge())
        findings = ablate.criticality(SORT, base, judge)
        critical = [f["index"] for f in findings if f["critical"]]
        self.assertEqual(critical, [12, 13, 14, 15])

    def test_an_intervention_forces_its_call_and_lets_the_rest_run(self):
        """The distinction between a counterfactual and a substitution.

        Only call 0 is forced. Everything after it goes to the real oracle, so
        the program is free to ask *different* questions than it did in the
        baseline - and after a forced swap it does exactly that. Identical
        downstream answers would mean the intervention had been ignored.
        """
        base = ablate.record(SORT, judge())
        forced = "1" if base.answers[0] == "2" else "2"
        recorder = ablate._Recorder(ablate._Intervention(judge(), 0, forced))
        ablate._execute(SORT, recorder)

        self.assertEqual(recorder.answers[0], forced)
        self.assertEqual(len(recorder.answers), len(base.answers))
        self.assertNotEqual(recorder.prompts[3:], base.prompts[3:])


class TestPlacebo(unittest.TestCase):

    def test_noise_does_not_reproduce_a_real_sort(self):
        base = ablate.record(SORT, judge())
        stats = ablate.placebo(SORT, base, trials=20)
        self.assertEqual(stats["matches"], 0)
        self.assertGreater(stats["mean_distance"], 1)

    def test_a_program_with_one_call_still_reports(self):
        base = ablate.record(HELLO, build_oracle("echo"))
        stats = ablate.placebo(HELLO, base, trials=5)
        self.assertEqual(stats["trials"], 5)


class TestSelfCorrection(unittest.TestCase):

    def test_the_first_calls_are_free(self):
        base = ablate.record(SORT, judge())
        curve = ablate.noise_curve(SORT, base, judge, trials=20)
        self.assertEqual(curve[0]["rate"], 1.0)      # no noise, no change
        self.assertGreaterEqual(ablate.free_prefix(curve), 3)

    def test_the_curve_ends_broken(self):
        base = ablate.record(SORT, judge())
        curve = ablate.noise_curve(SORT, base, judge, trials=20)
        self.assertEqual(curve[-1]["rate"], 0.0)     # all noise, never right

    def test_free_prefix_is_zero_when_the_first_call_matters(self):
        curve = [{"k": 0, "rate": 1.0}, {"k": 1, "rate": 0.5}]
        self.assertEqual(ablate.free_prefix(curve), 0)


class TestReport(unittest.TestCase):

    def test_the_report_names_all_three_measurements(self):
        buffer = io.StringIO()
        ablate.report(SORT, judge, trials=20, out=buffer)
        text = buffer.getvalue()
        for heading in ("BASELINE", "CRITICALITY", "PLACEBO",
                        "SELF CORRECTION", "VERDICT"):
            self.assertIn(heading, text)

    def test_a_program_with_no_traps_is_handled(self):
        buffer = io.StringIO()
        ablate.report("programs/hello.asm", judge, trials=2, out=buffer)
        self.assertIn("no oracle calls", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
