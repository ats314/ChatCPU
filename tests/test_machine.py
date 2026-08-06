import unittest

from trapcpu import Machine, MachineError, State, assemble, assemble_file
from trapcpu.oracle import EchoOracle, Fault, FaultInjector, ScriptedOracle
from trapcpu.protocol import (
    DESCRIPTOR_SIZE,
    FIELD_ATTEMPTS,
    FIELD_CORRECTED,
    FIELD_LENGTH,
    FIELD_MAGIC,
    FIELD_NONCE,
    FIELD_STATUS,
    FIELD_VALUE,
    Flag,
    Mode,
    Status,
    pack_descriptor,
    render_reply,
)

DESC = 0x0300
PROMPT = 0x0400
BUFFER = 0x0500

TRAP_PROGRAM = f"""
        LDIA 0x{DESC:04X}
        OUTP 0x30
        TRAP
        HLT
"""


def build(prompt="Name a colour. [hint: BLUE]", capacity=32, mode=Mode.TEXT,
          replicas=1, retries=0, flags=0, seed=5, budget=64, descriptor=DESC):
    machine = Machine(seed=seed, oracle_budget=budget)
    machine.load(assemble(TRAP_PROGRAM.replace(f"0x{DESC:04X}",
                                               f"0x{descriptor:04X}")))
    blob = pack_descriptor(PROMPT, BUFFER, capacity, mode, replicas, retries, flags)
    machine.ram[descriptor:descriptor + DESCRIPTOR_SIZE] = blob
    encoded = prompt.encode("utf-8")
    machine.ram[PROMPT:PROMPT + len(encoded)] = encoded
    machine.ram[PROMPT + len(encoded)] = 0
    return machine


def answer(machine, result, payload, checksum="crc"):
    return machine.resume(
        render_reply(result.frame.nonce, [payload], checksum=checksum)
    )


class TestTrapMechanics(unittest.TestCase):

    def test_trap_suspends_the_machine(self):
        machine = build()
        result = machine.run()
        self.assertEqual(result.state, State.TRAPPED)
        self.assertEqual(machine.state, State.TRAPPED)
        self.assertIsNotNone(result.frame)

    def test_the_frame_carries_the_prompt_and_machine_state(self):
        machine = build()
        frame = machine.run().frame
        self.assertIn("Name a colour", frame.prompt)
        self.assertEqual(frame.capacity, 32)
        self.assertEqual(frame.registers["A"], DESC)

    def test_resume_writes_the_payload_into_ram(self):
        machine = build()
        result = machine.run()
        result = answer(machine, result, "BLUE")
        self.assertEqual(result.state, State.HALTED)
        self.assertEqual(bytes(machine.ram[BUFFER:BUFFER + 5]), b"BLUE\x00")

    def test_registers_report_the_outcome(self):
        machine = build()
        answer(machine, machine.run(), "BLUE")
        self.assertEqual(machine.A, Status.OK)
        self.assertEqual(machine.B, 4)
        self.assertEqual(machine.C, 1)
        self.assertEqual(machine.CF, 0)

    def test_descriptor_writeback(self):
        machine = build()
        result = machine.run()
        nonce = result.frame.nonce
        answer(machine, result, "BLUE")
        self.assertEqual(machine.read16(DESC + FIELD_STATUS), Status.OK)
        self.assertEqual(machine.read16(DESC + FIELD_LENGTH), 4)
        self.assertEqual(machine.read16(DESC + FIELD_NONCE), nonce)
        self.assertEqual(machine.read8(DESC + FIELD_ATTEMPTS), 1)

    def test_num_mode_lands_in_d_and_the_descriptor(self):
        machine = build(mode=Mode.NUM, capacity=8)
        answer(machine, machine.run(), "4242")
        self.assertEqual(machine.D, 4242)
        self.assertEqual(machine.read16(DESC + FIELD_VALUE), 4242)
        self.assertEqual(machine.read16(BUFFER), 4242)

    def test_carry_flags_failure(self):
        machine = build()
        result = machine.run()
        machine.resume("nothing useful here")
        self.assertEqual(machine.CF, 1)
        self.assertEqual(machine.A, Status.NO_FRAME)

    def test_no_nul_flag_suppresses_the_terminator(self):
        machine = build(flags=Flag.NO_NUL)
        machine.ram[BUFFER + 4] = 0xFF
        answer(machine, machine.run(), "BLUE")
        self.assertEqual(machine.ram[BUFFER + 4], 0xFF)

    def test_resume_without_a_trap_is_an_error(self):
        machine = build()
        with self.assertRaises(MachineError):
            machine.resume("anything")

    def test_run_while_trapped_is_an_error(self):
        machine = build()
        machine.run()
        with self.assertRaises(MachineError):
            machine.run()

    def test_the_oracle_status_port_reports_the_last_result(self):
        source = f"""
                LDIA 0x{DESC:04X}
                OUTP 0x30
                TRAP
                INP 0x31
                STA 0x0600
                HLT
        """
        machine = Machine(seed=5)
        machine.load(assemble(source))
        machine.ram[DESC:DESC + DESCRIPTOR_SIZE] = pack_descriptor(
            PROMPT, BUFFER, 32
        )
        machine.ram[PROMPT:PROMPT + 5] = b"hi?\x00\x00"
        answer(machine, machine.run(), "yes")
        self.assertEqual(machine.read16(0x0600), Status.OK)


class TestDescriptorValidation(unittest.TestCase):

    def _expect(self, machine, status):
        result = machine.run()
        self.assertEqual(result.state, State.HALTED)
        self.assertEqual(machine.A, status)
        return machine

    def test_trap_without_a_latched_pointer(self):
        machine = Machine(seed=1)
        machine.load(assemble("TRAP\nHLT\n"))
        self._expect(machine, Status.NO_REQUEST)

    def test_bad_magic(self):
        machine = build()
        machine.write16(DESC + FIELD_MAGIC, 0x0000)
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_bad_version(self):
        machine = build()
        machine.write8(DESC + 0x02, 9)
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_unknown_mode(self):
        machine = build()
        machine.write8(DESC + 0x03, 7)
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_too_many_replicas(self):
        machine = build()
        machine.write8(DESC + 0x0A, 99)
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_zero_capacity(self):
        machine = build(capacity=0)
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_empty_prompt(self):
        machine = build()
        machine.ram[PROMPT] = 0
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_unterminated_prompt(self):
        machine = build()
        for offset in range(PROMPT, PROMPT + 5000):
            machine.ram[offset & 0xFFFF] = 0x41
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_response_buffer_overruns_ram(self):
        machine = build()
        machine.write16(DESC + 0x06, 0xFFF0)
        machine.write16(DESC + 0x08, 0x0100)
        self._expect(machine, Status.BAD_DESCRIPTOR)

    def test_a_failed_descriptor_never_suspends(self):
        machine = build()
        machine.write16(DESC + FIELD_MAGIC, 0)
        result = machine.run()
        self.assertEqual(result.state, State.HALTED)
        self.assertEqual(machine.stats.traps, 1)
        self.assertEqual(machine.stats.failed, 1)


class TestRetries(unittest.TestCase):

    def test_a_failure_with_retries_left_re_traps(self):
        machine = build(retries=2)
        first = machine.run()
        second = machine.resume("garbage")
        self.assertEqual(second.state, State.TRAPPED)
        self.assertEqual(second.frame.attempt, 2)

    def test_each_attempt_gets_a_fresh_nonce(self):
        machine = build(retries=3)
        seen = set()
        result = machine.run()
        for _ in range(4):
            seen.add(result.frame.nonce)
            result = machine.resume("garbage")
            if not result.trapped:
                break
        self.assertEqual(len(seen), 4)

    def test_a_stale_reply_cannot_satisfy_a_retry(self):
        machine = build(retries=1)
        first = machine.run()
        stale = render_reply(first.frame.nonce, ["BLUE"], checksum="crc")
        second = machine.resume("garbage")
        self.assertTrue(second.trapped)
        final = machine.resume(stale)  # answers the previous attempt
        self.assertEqual(machine.A, Status.BAD_NONCE)
        self.assertEqual(final.state, State.HALTED)

    def test_retries_are_exhausted_into_the_specific_status(self):
        machine = build(retries=2)
        result = machine.run()
        for _ in range(3):
            result = machine.resume("no frame at all")
        self.assertEqual(result.state, State.HALTED)
        self.assertEqual(machine.A, Status.NO_FRAME)
        self.assertEqual(machine.stats.retried, 2)
        self.assertEqual(machine.stats.attempts, 3)

    def test_recovery_after_a_bad_attempt(self):
        machine = build(retries=2)
        machine.execute(FaultInjector(EchoOracle(), [Fault.STALE_NONCE]))
        self.assertEqual(machine.A, Status.OK)
        self.assertEqual(machine.C, 2)
        self.assertEqual(machine.stats.retried, 1)

    def test_attempts_are_recorded_in_the_descriptor(self):
        machine = build(retries=3)
        machine.execute(
            FaultInjector(EchoOracle(), [Fault.BAD_LENGTH, Fault.BAD_CHECKSUM])
        )
        self.assertEqual(machine.read8(DESC + FIELD_ATTEMPTS), 3)


class TestBudgetAndAbort(unittest.TestCase):

    def test_budget_zero_refuses_to_trap(self):
        machine = build(budget=0)
        result = machine.run()
        self.assertEqual(result.state, State.HALTED)
        self.assertEqual(machine.A, Status.BUDGET)

    def test_budget_is_consumed_per_trap_not_per_attempt(self):
        machine = build(retries=3, budget=1)
        machine.execute(FaultInjector(EchoOracle(), [Fault.BAD_LENGTH]))
        self.assertEqual(machine.A, Status.OK)
        self.assertEqual(machine.traps_used, 1)

    def test_host_can_abort_a_pending_trap(self):
        machine = build()
        machine.run()
        result = machine.fail_trap(Status.ABORT, "operator gave up")
        self.assertEqual(result.state, State.HALTED)
        self.assertEqual(machine.A, Status.ABORT)

    def test_an_oracle_that_never_answers(self):
        class Silent:
            def ask(self, frame):
                return None

        machine = build()
        machine.execute(Silent())
        self.assertEqual(machine.A, Status.RETRIES)


class TestEccPath(unittest.TestCase):

    def test_corrected_bytes_are_reported(self):
        machine = build(replicas=3)
        machine.execute(ScriptedOracle([["BLUE", "BLXE", "BLUE"]]))
        self.assertEqual(machine.A, Status.DEGRADED)
        self.assertEqual(machine.read8(DESC + FIELD_CORRECTED), 1)
        self.assertEqual(bytes(machine.ram[BUFFER:BUFFER + 4]), b"BLUE")
        self.assertEqual(machine.stats.corrected, 1)

    def test_no_quorum_leaves_the_buffer_untouched(self):
        machine = build(replicas=3)
        machine.ram[BUFFER:BUFFER + 4] = b"\xAA\xAA\xAA\xAA"
        machine.execute(ScriptedOracle([["AAAA", "BBBB", "CCCC"]]))
        self.assertEqual(machine.A, Status.NO_QUORUM)
        self.assertEqual(bytes(machine.ram[BUFFER:BUFFER + 4]), b"\xAA" * 4)

    def test_length_outlier_is_discarded_and_counted(self):
        machine = build(replicas=3)
        machine.execute(ScriptedOracle([["BLUE", "BLUE", "BLUEISH"]]))
        self.assertTrue(machine.A in (Status.OK, Status.DEGRADED))
        self.assertEqual(machine.stats.discarded, 1)


class TestStatistics(unittest.TestCase):

    def test_counters_add_up(self):
        machine = build(retries=2)
        machine.execute(
            FaultInjector(EchoOracle(), [Fault.SILENCE, Fault.BAD_CHECKSUM])
        )
        stats = machine.stats
        self.assertEqual(stats.traps, 1)
        self.assertEqual(stats.attempts, 3)
        self.assertEqual(stats.ok + stats.degraded + stats.failed, 1)
        self.assertEqual(stats.by_status[Status.NO_FRAME], 1)
        self.assertEqual(stats.by_status[Status.BAD_CHECKSUM], 1)

    def test_report_mentions_every_outcome(self):
        machine = build()
        machine.execute(EchoOracle())
        self.assertIn("traps issued", machine.stats.report())
        self.assertIn("OK", machine.stats.report())


if __name__ == "__main__":
    unittest.main()


class TestSegmentLimit(unittest.TestCase):
    """A cycle budget must survive a trap, not silently reset to the default."""

    LOOPING = f"""
            LDIA 0x{DESC:04X}
            OUTP 0x30
            TRAP
    LOOP:   JMP LOOP
    """

    def _machine(self):
        machine = Machine(seed=5)
        machine.load(assemble(self.LOOPING))
        machine.ram[DESC:DESC + DESCRIPTOR_SIZE] = pack_descriptor(
            PROMPT, BUFFER, 32
        )
        machine.ram[PROMPT:PROMPT + 4] = b"hi?\x00"
        return machine

    def test_the_limit_carries_across_a_resume(self):
        machine = self._machine()
        result = machine.run(limit=200)
        self.assertTrue(result.trapped)

        result = answer(machine, result, "BLUE")
        self.assertEqual(result.state, State.LIMIT)
        self.assertLess(machine.cycles, 500)

    def test_execute_honours_its_limit_after_a_trap(self):
        machine = self._machine()
        machine.execute(EchoOracle(), limit=200)
        self.assertEqual(machine.state, State.LIMIT)
        self.assertLess(machine.cycles, 500)


class TestTrapRegisterABI(unittest.TestCase):
    """TRAP returns through B, C and D as well as A.

    Discovered the hard way: oracle_sort.asm held a loop index in C across a
    trap, and the trap overwrote it with the attempt count. Nothing faulted -
    the sort just silently produced a corrupted list. These tests pin the
    contract so the clobber is a documented ABI rather than a trap for the
    next caller.
    """

    SRC = """
.INCLUDE "trap.inc"
.DATA
.ORG 0x0300
REQ:  .DW ORACLE_MAGIC
      .DB ORACLE_VERSION
      .DB MODE_NUM
      .DW P
      .DW ANS
      .DW 8
      .DB 1
      .DB 2
      .DW 0
      .DW 0
      .DW 0
      .DB 0
      .DB 0
      .DW 0
      .DW 0
P:    .ASCIIZ "pick a number [hint: 137]"
ANS:  .RESB 8
.CODE
      LDIB 0xBBBB
      LDIC 0xCCCC
      LDID 0xDDDD
      LDIA REQ
      OUTP PORT_ORACLE
      TRAP
      HLT
"""

    def _run(self):
        import os
        import tempfile
        from trapcpu.cli import build_oracle
        directory = os.path.join(os.getcwd(), "programs", "trap")
        handle = tempfile.NamedTemporaryFile("w", suffix=".asm", dir=directory,
                                             delete=False)
        try:
            handle.write(self.SRC)
            handle.close()
            machine = Machine(seed=1)
            machine.load(assemble_file(handle.name))
            machine.execute(build_oracle("echo"))
            return machine
        finally:
            os.unlink(handle.name)

    def test_the_parsed_value_arrives_in_d(self):
        self.assertEqual(self._run().D, 137)

    def test_b_and_c_are_clobbered_not_preserved(self):
        """The surprising half of the contract, stated as a test."""
        machine = self._run()
        self.assertNotEqual(machine.B, 0xBBBB)
        self.assertNotEqual(machine.C, 0xCCCC)
        self.assertEqual(machine.A, 0)          # ST_OK
        self.assertEqual(machine.C, 1)          # attempts, not the caller's C
