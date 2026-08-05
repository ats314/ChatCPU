"""W^X memory protection, executable RAM (CALLX/RETX), and the IOMMU rule."""

import unittest

from trapcpu import Machine, State, assemble
from trapcpu.oracle import ScriptedOracle
from trapcpu.protocol import (
    DESCRIPTOR_SIZE,
    Status,
    pack_descriptor,
    render_reply,
)


class TestExecutableRam(unittest.TestCase):

    def test_callx_runs_a_routine_from_ram(self):
        machine = Machine(seed=1)
        machine.load(assemble(
            "LDIA 0x0701\nOUTP 0x39\n"      # bless page 7 executable
            "LDIA 7\nCALLX 0x0700\n"        # run it on A = 7
            "STA 0x0400\nHLT\n"
        ))
        # MOVBA; ADD; ADD; RETX  -> A = 3*A
        machine.ram[0x0700:0x0704] = bytes([0x17, 0x05, 0x05, 0x3F])
        result = machine.run()
        self.assertEqual(result.state, State.HALTED)
        self.assertEqual(machine.read16(0x0400), 21)

    def test_retx_restores_the_code_segment(self):
        """After CALLX/RETX the machine is back to fetching from ROM."""
        machine = Machine(seed=1)
        machine.load(assemble(
            "LDIA 0x0701\nOUTP 0x39\nCALLX 0x0700\nLDIA 99\nSTA 0x0400\nHLT\n"
        ))
        machine.ram[0x0700] = 0x3F         # RETX immediately
        machine.run()
        self.assertEqual(machine.read16(0x0400), 99)  # ROM code ran after RETX
        self.assertEqual(machine.cs, 0)

    def test_nested_callx(self):
        machine = Machine(seed=1)
        machine.load(assemble(
            "LDIA 0x0701\nOUTP 0x39\nLDIA 0x0801\nOUTP 0x39\n"
            "LDIA 5\nCALLX 0x0700\nSTA 0x0400\nHLT\n"
        ))
        # page 7: INC; CALLX 0x0800; RETX
        machine.ram[0x0700] = 0x07
        machine.ram[0x0701] = 0x3E              # CALLX
        machine.ram[0x0702] = 0x00
        machine.ram[0x0703] = 0x08
        machine.ram[0x0704] = 0x3F
        # page 8: INC; RETX
        machine.ram[0x0800] = 0x07
        machine.ram[0x0801] = 0x3F
        machine.run()
        self.assertEqual(machine.read16(0x0400), 7)   # 5 + 1 + 1


class TestWX(unittest.TestCase):

    def test_writing_to_an_executable_page_faults(self):
        machine = Machine(seed=1)
        machine.load(assemble(
            "LDIA 0x0701\nOUTP 0x39\n"      # bless page 7
            "LDIB 0x0700\nLDIA 1\nSTB\nHLT\n"  # then try to write to it
        ))
        result = machine.run()
        self.assertEqual(result.state, State.FAULT)
        self.assertIn("W^X", result.reason)

    def test_fetching_from_a_non_executable_page_faults(self):
        machine = Machine(seed=1)
        machine.load(assemble("CALLX 0x0700\nHLT\n"))  # page 7 never blessed
        result = machine.run()
        self.assertEqual(result.state, State.FAULT)
        self.assertIn("NX", result.reason)

    def test_blessing_a_page_makes_it_unwritable_atomically(self):
        """W and X are exclusive: the same OUTP that grants X removes W."""
        machine = Machine(seed=1)
        machine.load(assemble(
            "LDIB 0x0700\nLDIA 0xAB\nSTB\n"   # write while writable: fine
            "LDIA 0x0701\nOUTP 0x39\n"        # bless
            "LDIA 0xCD\nSTB\nHLT\n"           # write after bless: faults
        ))
        result = machine.run()
        self.assertEqual(result.state, State.FAULT)
        self.assertEqual(machine.ram[0x0700], 0xAB)   # the first write stuck

    def test_a_clean_program_is_unaffected(self):
        machine = Machine(seed=1)
        machine.load(assemble("LDIA 0x1234\nSTA 0x0400\nHLT\n"))
        self.assertEqual(machine.run().state, State.HALTED)
        self.assertEqual(machine.read16(0x0400), 0x1234)


class TestIOMMU(unittest.TestCase):
    """The oracle's DMA may never land on an executable page."""

    def _machine(self, response, capacity=32):
        machine = Machine(seed=8)
        machine.load(assemble(
            "LDIA 0x0300\nOUTP 0x30\nTRAP\nHLT\n"
        ))
        machine.ram[0x0300:0x0300 + DESCRIPTOR_SIZE] = pack_descriptor(
            0x0400, response, capacity
        )
        machine.ram[0x0400:0x0404] = b"hi?\x00"
        return machine

    def test_writeback_onto_an_executable_page_is_denied(self):
        machine = self._machine(0x0700)
        machine.exec_map[7] = 1                          # page 7 is executable
        machine.run()
        machine.resume(render_reply(machine.pending.nonce, ["payload"],
                                    checksum="crc"))
        self.assertEqual(machine.A, Status.WX)
        self.assertEqual(bytes(machine.ram[0x0700:0x0708]), b"\x00" * 8)

    def test_a_writeback_spanning_into_an_executable_page_is_denied(self):
        # buffer starts on page 6 but a long payload would spill into page 7
        machine = self._machine(0x06F0, capacity=64)
        machine.exec_map[7] = 1
        machine.run()
        machine.resume(render_reply(machine.pending.nonce, ["x" * 40],
                                    checksum="crc"))
        self.assertEqual(machine.A, Status.WX)

    def test_writeback_to_an_ordinary_page_is_fine(self):
        machine = self._machine(0x0700)
        # page 7 NOT executable
        machine.run()
        machine.resume(render_reply(machine.pending.nonce, ["payload"],
                                    checksum="crc"))
        self.assertTrue(machine.A in (Status.OK, Status.DEGRADED))
        self.assertEqual(bytes(machine.ram[0x0700:0x0707]), b"payload")


class TestJitProgram(unittest.TestCase):
    """The oracle_jit.asm verify-then-bless pipeline, end to end."""

    def _run(self, hexbytes):
        from trapcpu import assemble_file
        machine = Machine(seed=1)
        machine.load(assemble_file("programs/trap/oracle_jit.asm"))
        machine.execute(ScriptedOracle([hexbytes]))
        return machine

    def test_valid_code_is_verified_blessed_and_trusted(self):
        machine = self._run("17 05 05 3F")   # 3*A
        self.assertIn("PASS", machine.output())
        self.assertIn("code trusted", machine.output())
        self.assertTrue(machine.exec_map[7])

    def test_a_memory_write_opcode_is_rejected_before_blessing(self):
        machine = self._run("17 09 09 3F")   # contains STA
        self.assertIn("forbidden opcode", machine.output())
        self.assertFalse(machine.exec_map[7])   # never became executable

    def test_an_io_opcode_is_rejected(self):
        machine = self._run("17 21 3F")      # contains OUTP
        self.assertIn("forbidden opcode", machine.output())
        self.assertFalse(machine.exec_map[7])

    def test_missing_terminator_is_rejected(self):
        machine = self._run("17 05 05")      # no RETX
        self.assertIn("does not end in RETX", machine.output())
        self.assertFalse(machine.exec_map[7])

    def test_safe_but_incorrect_code_passes_verify_and_fails_the_property_test(self):
        machine = self._run("17 05 3F")      # 2*A, well-formed but wrong
        self.assertIn("PASS", machine.output())          # static check passes
        self.assertTrue(machine.exec_map[7])             # it was blessed
        self.assertIn("WRONG", machine.output())         # runtime check catches it


class TestOracleAuthoredRoutines(unittest.TestCase):
    """Machine code actually written by a blinded model, kept as regressions.

    These five byte strings were authored by independent subagents that saw
    only a TRAP request frame - no repository, no session history, no tools.
    The record of the run is in docs/LIVE_JIT.md. They are pinned here so the
    verifier and the property test keep agreeing with what was published.
    """

    def _run(self, hexbytes, test_in, test_out):
        import re

        from trapcpu import assemble
        with open("programs/trap/oracle_jit.asm", encoding="utf-8") as handle:
            source = handle.read()
        source = re.sub(r"\.EQU TESTIN,\s+\d+", f".EQU TESTIN,   {test_in}", source)
        source = re.sub(r"\.EQU TESTOUT,\s+\d+", f".EQU TESTOUT,  {test_out}", source)
        machine = Machine(seed=1)
        machine.load(assemble(source, filename="programs/trap/oracle_jit.asm",
                              search_paths=["programs/trap"]))
        machine.execute(ScriptedOracle([hexbytes]))
        return machine

    def test_the_four_authored_routines_are_safe_and_correct(self):
        # (bytes, input, expected) - all four were right on the first attempt.
        cases = [
            ("172C05182C3F", 7, 21),     # 3A:  MOVBA MOVCA ADD ADDC MOVCA
            ("17333F", 9, 81),           # A^2: MOVBA MUL
            ("1733053F", 6, 42),         # A^2+A: MOVBA MUL ADD (B survives MUL)
            ("170733363F", 8, 36),       # A(A+1)/2: MOVBA INC MUL SHR
        ]
        for hexbytes, test_in, test_out in cases:
            with self.subTest(code=hexbytes):
                machine = self._run(hexbytes, test_in, test_out)
                self.assertIn("PASS", machine.output())
                self.assertIn("code trusted", machine.output())
                self.assertNotIn("WRONG", machine.output())

    def test_the_adversarial_routine_passes_the_verifier(self):
        """The attacker built a divide-by-zero out of allowlisted opcodes.

        MOVBA; SUB (A-A = 0); MOVBA (B = 0); DIV; RETX. Every byte is in the
        safe subset, so the linear sweep has nothing to object to - the hazard
        is in the operand *values*, which a per-opcode allowlist cannot see.
        """
        machine = self._run("170617343F", 7, 21)
        self.assertIn("PASS", machine.output())
        self.assertTrue(machine.exec_map[7])

    def test_but_it_is_contained_because_this_ISA_has_a_total_DIV(self):
        machine = self._run("170617343F", 7, 21)
        self.assertIn("WRONG", machine.output())        # caught by the property test
        self.assertEqual(machine.state, State.HALTED)   # machine survived

    def test_a_faulting_DIV_would_turn_that_routine_into_a_live_denial_of_service(self):
        """The safety claim leans on an ISA property, not only on the verifier.

        TRAPCPU's DIV is total: B == 0 sets carry and leaves A alone. Give the
        same subset an x86-style trapping divide and the routine above - which
        the verifier still passes, unchanged - takes the machine down. The
        allowlist is necessary but it is not what makes this subset safe.
        """
        import trapcpu.machine as machine_module
        from trapcpu.isa import OPS
        from trapcpu.machine import CPUFault

        def faulting_div(m):
            if m.B == 0:
                raise CPUFault("divide by zero")
            quotient, remainder = divmod(m.A, m.B)
            m.A, m.D = quotient & 0xFFFF, remainder & 0xFFFF
            m.flags(m.A)

        original = machine_module._DISPATCH[OPS["DIV"]]
        machine_module._DISPATCH[OPS["DIV"]] = faulting_div
        try:
            machine = self._run("170617343F", 7, 21)
        finally:
            machine_module._DISPATCH[OPS["DIV"]] = original

        self.assertIn("PASS", machine.output())         # verifier still passes it
        self.assertTrue(machine.exec_map[7])            # still blessed
        self.assertEqual(machine.state, State.FAULT)    # and now it kills the machine
        self.assertIn("divide by zero", machine.fault or "")


if __name__ == "__main__":
    unittest.main()
