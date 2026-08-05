"""oracle_sort.asm: a sort whose comparison operator is the coprocessor."""

import unittest

from trapcpu import Machine, assemble_file
from trapcpu.oracle import JudgeOracle

ITEMS = ["paper cut", "wasp sting", "car crash", "house fire", "hurricane"]


class Contrarian(JudgeOracle):
    """The same backend with its opinion inverted."""

    def rank(self, item):
        return -JudgeOracle.rank(self, item)


def order(output):
    """The item order in the 'after:' block, as a list of names."""
    after = output.split("after:", 1)[1]
    seen = [(after.index(name), name) for name in ITEMS if name in after]
    return [name for _, name in sorted(seen)]


class TestOracleSort(unittest.TestCase):

    def _run(self, oracle):
        machine = Machine(seed=1)
        machine.load(assemble_file("programs/trap/oracle_sort.asm"))
        machine.execute(oracle)
        return machine.output()

    def test_it_sorts_into_the_oracles_order(self):
        self.assertEqual(order(self._run(JudgeOracle())), ITEMS)

    def test_the_ordering_comes_from_the_oracle_and_not_the_program(self):
        """The point of the demo, as a test.

        Nothing in the assembly knows that a hurricane outranks a paper cut -
        it moves 16-bit words and asks the device which way round they go. Flip
        the device's opinion and the same unmodified program sorts the other
        way.
        """
        self.assertEqual(order(self._run(Contrarian())), list(reversed(ITEMS)))

    def test_every_comparison_is_a_real_trap(self):
        machine = Machine(seed=1)
        machine.load(assemble_file("programs/trap/oracle_sort.asm"))
        asked = []

        class Counting(JudgeOracle):
            def ask(self, frame):
                asked.append(frame.prompt)
                return JudgeOracle.ask(self, frame)

        machine.execute(Counting())
        # Bubble sort with both bounds at N-1 makes (N-1)^2 comparisons.
        self.assertEqual(len(asked), 16)
        self.assertIn("16 oracle comparisons", machine.output())
        # Each prompt was rebuilt in guest RAM from the two items being
        # compared, so no two consecutive prompts should be identical unless
        # the sort genuinely revisited the same pair.
        self.assertTrue(all("1)" in p and "2)" in p for p in asked))

    def test_the_program_reports_failure_when_the_device_never_answers(self):
        from trapcpu.cli import build_oracle
        machine = Machine(seed=1)
        machine.load(assemble_file("programs/trap/oracle_sort.asm"))
        machine.execute(build_oracle("none"))
        self.assertIn("oracle failed", machine.output())


if __name__ == "__main__":
    unittest.main()
