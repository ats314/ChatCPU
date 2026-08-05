"""The demo programs are part of the deliverable, so they get tested too."""

import glob
import os
import unittest

from trapcpu import Machine, State, assemble_file
from trapcpu.oracle import BisectOracle, EchoOracle, Fault, FaultInjector
from trapcpu.protocol import FIELD_CORRECTED, Status
from trapcpu.snapshot import dump, mount

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRAMS = os.path.join(HERE, "programs", "trap")


def path(name):
    return os.path.join(PROGRAMS, name)


def run(name, oracle=None, seed=1, budget=64):
    machine = Machine(seed=seed, oracle_budget=budget)
    machine.load(assemble_file(path(name)))
    result = machine.execute(oracle or EchoOracle())
    return machine, result


class TestEverythingAssembles(unittest.TestCase):

    def test_all_demo_programs_assemble(self):
        sources = sorted(glob.glob(os.path.join(PROGRAMS, "*.asm")))
        self.assertTrue(sources, "no demo programs found")
        for source in sources:
            with self.subTest(program=os.path.basename(source)):
                program = assemble_file(source)
                self.assertTrue(program.code)
                self.assertTrue(program.data)

    def test_legacy_chatcpu_programs_still_assemble(self):
        """The base ISA is unchanged, so the old hello world must still build."""
        legacy = os.path.join(HERE, "programs", "hello.asm")
        if not os.path.exists(legacy):
            self.skipTest("legacy program not present")
        program = assemble_file(legacy)
        self.assertTrue(program.code)


class TestOracleHello(unittest.TestCase):

    def test_it_prints_what_the_oracle_said(self):
        machine, result = run("oracle_hello.asm")
        self.assertEqual(result.state, State.HALTED)
        self.assertIn("HELLO FROM THE OTHER SIDE OF THE BUS", machine.output())
        self.assertEqual(machine.stats.traps, 1)

    def test_it_takes_the_failure_branch_when_the_oracle_never_answers(self):
        class Silent:
            def ask(self, frame):
                return None

        machine, _ = run("oracle_hello.asm", Silent())
        self.assertIn("oracle failed", machine.output())
        self.assertEqual(machine.A, Status.RETRIES)

    def test_it_recovers_from_a_bad_first_attempt(self):
        oracle = FaultInjector(EchoOracle(), [Fault.STALE_NONCE])
        machine, _ = run("oracle_hello.asm", oracle)
        self.assertIn("oracle says", machine.output())
        self.assertEqual(machine.stats.retried, 1)

    def test_two_retries_are_enough_for_two_bad_attempts(self):
        oracle = FaultInjector(EchoOracle(), [Fault.SILENCE, Fault.BAD_LENGTH])
        machine, _ = run("oracle_hello.asm", oracle)
        self.assertIn("oracle says", machine.output())
        self.assertEqual(machine.C, 3)

    def test_three_bad_attempts_exhaust_the_retries(self):
        oracle = FaultInjector(
            EchoOracle(), [Fault.SILENCE, Fault.SILENCE, Fault.SILENCE]
        )
        machine, _ = run("oracle_hello.asm", oracle)
        self.assertIn("oracle failed", machine.output())


class TestOracleNum(unittest.TestCase):

    def test_the_answer_reaches_the_alu(self):
        machine, result = run("oracle_num.asm")
        self.assertEqual(result.state, State.HALTED)
        self.assertIn("the oracle picked 137", machine.output())
        self.assertIn("137 squared is 18769", machine.output())

    def test_it_reports_the_attempt_count(self):
        oracle = FaultInjector(EchoOracle(), [Fault.BAD_CHECKSUM])
        machine, _ = run("oracle_num.asm", oracle)
        self.assertIn("it took 2 attempt(s)", machine.output())

    def test_prose_where_a_number_was_demanded_is_rejected(self):
        oracle = FaultInjector(
            EchoOracle(), [Fault.WRONG_MODE] * 4
        )
        machine, _ = run("oracle_num.asm", oracle)
        self.assertIn("oracle failed", machine.output())


class TestOracleEcc(unittest.TestCase):

    def test_a_clean_vote_repairs_nothing(self):
        machine, _ = run("oracle_ecc.asm")
        self.assertIn("elected answer : CORAL", machine.output())
        self.assertIn("bytes repaired : 0", machine.output())

    def test_a_corrupted_replica_is_outvoted_and_counted(self):
        """One replica is mangled; five samples means the majority still holds."""
        from trapcpu.oracle import Oracle
        from trapcpu.protocol import render_reply

        class OneBadReplica(Oracle):
            def ask(self, frame):
                answers = ["CORAL"] * frame.replicas
                answers[1] = "CXRAL"
                return render_reply(
                    frame.nonce, answers, checksum="crc", replicas=frame.replicas
                )

        machine, _ = run("oracle_ecc.asm", OneBadReplica())
        self.assertIn("elected answer : CORAL", machine.output())
        self.assertIn("bytes repaired : 1", machine.output())
        self.assertEqual(machine.read8(0x0300 + FIELD_CORRECTED), 1)

    def test_a_split_vote_is_uncorrectable(self):
        from trapcpu.oracle import Oracle
        from trapcpu.protocol import render_reply

        class AllDifferent(Oracle):
            def ask(self, frame):
                return render_reply(
                    frame.nonce,
                    ["AAAAA", "BBBBB", "CCCCC", "DDDDD", "EEEEE"],
                    checksum="crc", replicas=frame.replicas,
                )

        machine, _ = run("oracle_ecc.asm", AllDifferent())
        self.assertIn("uncorrectable", machine.output())
        self.assertIn("001A", machine.output())  # NO_QUORUM


class TestOracleGuess(unittest.TestCase):

    def test_the_opponent_wins_by_bisection(self):
        machine, result = run("oracle_guess.asm", BisectOracle(), seed=4)
        self.assertEqual(result.state, State.HALTED)
        self.assertIn("correct!", machine.output())
        self.assertLessEqual(machine.stats.traps, 8)

    def test_the_prompt_is_rebuilt_from_guest_memory_each_round(self):
        prompts = []

        class Watcher(BisectOracle):
            def ask(self, frame):
                prompts.append(frame.prompt)
                return super().ask(frame)

        run("oracle_guess.asm", Watcher(), seed=4)
        self.assertGreater(len(prompts), 1)
        self.assertIn("first guess", prompts[0])
        self.assertIn("previous guess", prompts[1])
        self.assertNotEqual(prompts[0], prompts[1])

    def test_a_stubborn_opponent_runs_out_of_guesses(self):
        machine, _ = run("oracle_guess.asm", EchoOracle(), seed=4)
        self.assertIn("ran out of guesses", machine.output())
        self.assertEqual(machine.stats.traps, 8)

    def test_the_budget_stops_a_runaway_game(self):
        machine, _ = run("oracle_guess.asm", EchoOracle(), seed=4, budget=3)
        self.assertIn("status 0x0022", machine.output())  # BUDGET

    def test_a_game_can_be_suspended_and_resumed_from_a_transcript(self):
        machine = Machine(seed=4)
        machine.load(assemble_file(path("oracle_guess.asm")))
        oracle = BisectOracle()

        result = machine.run()
        self.assertTrue(result.trapped)

        # Park the whole machine into text, throw the machine away, mount it
        # back from the transcript, and finish the game.
        transcript = "chat noise\n" + dump(machine) + "\nmore chat noise"
        restored, report = mount(transcript)
        self.assertTrue(report.clean)

        result = restored.resume(oracle.ask(restored.pending.to_frame(restored)))
        while result.trapped:
            result = restored.resume(oracle.ask(result.frame))

        self.assertEqual(result.state, State.HALTED)
        self.assertIn("correct!", restored.output())


if __name__ == "__main__":
    unittest.main()
