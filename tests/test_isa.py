import unittest

from trapcpu import Machine, State, assemble
from trapcpu.isa import ARG8_OPS, ARG16_OPS, MNEMONICS, OPS, width
from trapcpu.machine import _DISPATCH


def run(source, **kwargs):
    machine = Machine(seed=1, **kwargs)
    machine.load(assemble(source))
    result = machine.run()
    return machine, result


class TestOpcodeTable(unittest.TestCase):

    def test_opcodes_are_unique(self):
        self.assertEqual(len(set(OPS.values())), len(OPS))

    def test_every_mnemonic_has_an_implementation(self):
        self.assertEqual(set(_DISPATCH), set(OPS.values()))

    def test_reverse_table_round_trips(self):
        for name, opcode in OPS.items():
            self.assertEqual(MNEMONICS[opcode], name)

    def test_widths(self):
        self.assertEqual(width("HLT"), 1)
        self.assertEqual(width("OUTP"), 2)
        self.assertEqual(width("LDIA"), 3)

    def test_operand_classes_are_disjoint(self):
        self.assertFalse(ARG8_OPS & ARG16_OPS)


class TestArithmetic(unittest.TestCase):

    def test_add_sets_carry_on_overflow(self):
        machine, _ = run("LDIA 0xFFFF\nLDIB 2\nADD\nHLT\n")
        self.assertEqual(machine.A, 1)
        self.assertEqual(machine.CF, 1)

    def test_sub_sets_carry_on_borrow(self):
        machine, _ = run("LDIA 1\nLDIB 2\nSUB\nHLT\n")
        self.assertEqual(machine.A, 0xFFFF)
        self.assertEqual(machine.CF, 1)
        self.assertEqual(machine.N, 1)

    def test_cmp_leaves_a_alone_but_sets_borrow(self):
        machine, _ = run("LDIA 1\nLDIB 2\nCMP\nHLT\n")
        self.assertEqual(machine.A, 1)
        self.assertEqual(machine.CF, 1)
        self.assertEqual(machine.Z, 0)

    def test_cmp_equal_sets_zero_and_clears_carry(self):
        machine, _ = run("LDIA 7\nLDIB 7\nCMP\nHLT\n")
        self.assertEqual((machine.Z, machine.CF), (1, 0))

    def test_mul(self):
        machine, _ = run("LDIA 300\nLDIB 300\nMUL\nHLT\n")
        self.assertEqual(machine.A, (300 * 300) & 0xFFFF)
        self.assertEqual(machine.CF, 1)

    def test_div_gives_quotient_and_remainder(self):
        machine, _ = run("LDIA 1234\nLDIB 10\nDIV\nHLT\n")
        self.assertEqual((machine.A, machine.D), (123, 4))
        self.assertEqual(machine.CF, 0)

    def test_div_by_zero_sets_carry_and_changes_nothing(self):
        machine, _ = run("LDIA 5\nLDIB 0\nDIV\nHLT\n")
        self.assertEqual(machine.A, 5)
        self.assertEqual(machine.CF, 1)

    def test_shifts(self):
        machine, _ = run("LDIA 0x8001\nSHL\nHLT\n")
        self.assertEqual((machine.A, machine.CF), (0x0002, 1))
        machine, _ = run("LDIA 0x8001\nSHR\nHLT\n")
        self.assertEqual((machine.A, machine.CF), (0x4000, 1))


class TestMemoryAndPointers(unittest.TestCase):

    def test_absolute_word_access(self):
        machine, _ = run("LDIA 0xBEEF\nSTA 0x0400\nLDIA 0\nLDA 0x0400\nHLT\n")
        self.assertEqual(machine.A, 0xBEEF)

    def test_little_endian_layout(self):
        machine, _ = run("LDIA 0x1234\nSTA 0x0400\nHLT\n")
        self.assertEqual(machine.ram[0x0400], 0x34)
        self.assertEqual(machine.ram[0x0401], 0x12)

    def test_indirect_byte_access_through_b(self):
        machine, _ = run(
            "LDIB 0x0500\nLDIA 'Z'\nSTB\nLDIA 0\nLDB\nHLT\n"
        )
        self.assertEqual(machine.A, ord("Z"))

    def test_indirect_word_access(self):
        machine, _ = run("LDIB 0x0500\nLDIA 0xCAFE\nSTW\nLDIA 0\nLDW\nHLT\n")
        self.assertEqual(machine.A, 0xCAFE)

    def test_ldb_sets_zero_flag_for_string_walks(self):
        machine, _ = run("LDIB 0x0500\nLDB\nHLT\n")
        self.assertEqual(machine.Z, 1)

    def test_pointer_increment_wraps(self):
        machine, _ = run("LDIB 0xFFFF\nINCB\nHLT\n")
        self.assertEqual(machine.B, 0)


class TestControlFlow(unittest.TestCase):

    def test_call_and_ret(self):
        machine, _ = run(
            "CALL SUB\nLDIB 1\nADD\nHLT\nSUB: LDIA 41\nRET\n"
        )
        self.assertEqual(machine.A, 42)

    def test_stack_is_last_in_first_out(self):
        machine, _ = run("LDIA 1\nPUSH\nLDIA 2\nPUSH\nPOP\nSTA 0x0400\nPOP\nHLT\n")
        self.assertEqual(machine.A, 1)
        self.assertEqual(machine.read16(0x0400), 2)

    def test_conditional_jumps(self):
        for source, expected in [
            ("LDIA 0\nJZ HIT\nLDIA 9\nHLT\nHIT: LDIA 1\nHLT\n", 1),
            ("LDIA 5\nJNZ HIT\nLDIA 9\nHLT\nHIT: LDIA 1\nHLT\n", 1),
            ("LDIA 0x8000\nJN HIT\nLDIA 9\nHLT\nHIT: LDIA 1\nHLT\n", 1),
            ("LDIA 1\nLDIB 2\nCMP\nJC HIT\nLDIA 9\nHLT\nHIT: LDIA 1\nHLT\n", 1),
            ("LDIA 2\nLDIB 1\nCMP\nJNC HIT\nLDIA 9\nHLT\nHIT: LDIA 1\nHLT\n", 1),
        ]:
            machine, _ = run(source)
            self.assertEqual(machine.A, expected, source)

    def test_invalid_opcode_faults_without_raising(self):
        machine = Machine(seed=1)
        machine.load(bytes([0xFE]))
        result = machine.run()
        self.assertEqual(result.state, State.FAULT)
        self.assertIn("invalid opcode FE", result.reason)

    def test_cycle_budget_stops_a_runaway(self):
        machine = Machine(seed=1)
        machine.load(assemble("LOOP: JMP LOOP\n"))
        result = machine.run(limit=500)
        self.assertEqual(result.state, State.LIMIT)
        self.assertEqual(machine.cycles, 500)

    def test_a_stopped_machine_can_be_resumed(self):
        machine = Machine(seed=1)
        machine.load(assemble("LDIC 3\nLOOP: DECC\nMOVAC\nJNZ LOOP\nHLT\n"))
        first = machine.run(limit=3)
        self.assertEqual(first.state, State.LIMIT)
        self.assertEqual(machine.run().state, State.HALTED)


class TestConsoleAndPorts(unittest.TestCase):

    def test_out_emits_characters(self):
        machine, _ = run("LDIA 'h'\nOUT\nLDIA 'i'\nOUT\nHLT\n")
        self.assertEqual(machine.output(), "hi")

    def test_outs_emits_a_whole_string(self):
        machine, _ = run(
            '.DATA\n.ORG 0x0400\nS: .ASCIIZ "abc"\n.CODE\nLDIB S\nOUTS\nHLT\n'
        )
        self.assertEqual(machine.output(), "abc")

    def test_screen_port_plots_at_b_and_c(self):
        machine, _ = run("LDIB 3\nLDIC 1\nLDIA '#'\nOUTP 0x10\nHLT\n")
        self.assertEqual(machine.hardware.screen[1][3], "#")

    def test_keyboard_queue(self):
        machine = Machine(seed=1)
        machine.load(assemble("INP 0x01\nSTA 0x0400\nINP 0x00\nHLT\n"))
        machine.hardware.key("q")
        machine.run()
        self.assertEqual(machine.read16(0x0400), 1)
        self.assertEqual(machine.A, ord("q"))

    def test_random_port_is_seeded(self):
        first, _ = run("INP 0x20\nHLT\n")
        second, _ = run("INP 0x20\nHLT\n")
        self.assertEqual(first.A, second.A)


if __name__ == "__main__":
    unittest.main()
