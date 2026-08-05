"""Async traps, interrupts, and the WFI/IRET cycle."""

import unittest

from trapcpu import Machine, State, assemble
from trapcpu.oracle import EchoOracle
from trapcpu.protocol import (
    DESCRIPTOR_SIZE,
    FIELD_VALUE,
    Status,
    pack_descriptor,
    render_reply,
)

DESC = 0x0300
PROMPT = 0x0400
BUFFER = 0x0500


def build(source, prompt="how much? [hint: 42]", capacity=8, mode=1, retries=0,
          seed=5):
    machine = Machine(seed=seed)
    machine.load(assemble(source))
    machine.ram[DESC:DESC + DESCRIPTOR_SIZE] = pack_descriptor(
        PROMPT, BUFFER, capacity, mode, 1, retries
    )
    blob = prompt.encode("utf-8")
    machine.ram[PROMPT:PROMPT + len(blob)] = blob
    machine.ram[PROMPT + len(blob)] = 0
    return machine


# Fire an async request, spin until an interrupt flips DONE, then halt.
ASYNC_LOOP = """
        LDIA HANDLER
        OUTP 0x38
        STI
        LDIA 0x0300
        OUTP 0x30
        TRAPA
LOOP:   LDA  TICKS
        INC
        STA  TICKS
        LDA  DONE
        JZ   LOOP
        HLT
HANDLER: PUSH
        LDIA 1
        STA  DONE
        POP
        IRET
.DATA
.ORG 0x0600
TICKS:  .DW 0
DONE:   .DW 0
"""


class TestAsyncLifecycle(unittest.TestCase):

    def test_trapa_yields_async_and_keeps_the_machine_runnable(self):
        machine = build(ASYNC_LOOP)
        result = machine.run()
        self.assertEqual(result.state, State.ASYNC)
        self.assertEqual(result.frame.channel, "ASYNC")
        self.assertIsNotNone(machine.async_pending)

    def test_the_machine_runs_during_the_async_wait(self):
        machine = build(ASYNC_LOOP)
        machine.run()
        interim = machine.run(300)          # the device is "thinking"
        self.assertEqual(interim.state, State.LIMIT)
        self.assertGreater(machine.read16(0x0600), 10)  # real work happened

    def test_completion_arrives_as_an_interrupt_not_a_register_clobber(self):
        machine = build(ASYNC_LOOP)
        result = machine.run()
        machine.run(50)
        ticks_before = machine.read16(0x0600)
        final = machine.resume(
            render_reply(machine.async_pending.nonce, ["42"], checksum="crc")
        )
        self.assertEqual(final.state, State.HALTED)
        self.assertEqual(machine.read16(0x0602), 1)          # DONE set by handler
        self.assertGreater(machine.read16(0x0600), ticks_before)
        self.assertEqual(machine.read16(DESC + FIELD_VALUE), 42)

    def test_execute_hides_latency(self):
        machine = build(ASYNC_LOOP)
        machine.execute(EchoOracle(), async_latency=500)
        self.assertGreater(machine.read16(0x0600), 20)
        self.assertEqual(machine.read16(DESC + FIELD_VALUE), 42)

    def test_zero_latency_still_completes(self):
        machine = build(ASYNC_LOOP)
        machine.execute(EchoOracle(), async_latency=0)
        self.assertEqual(machine.read16(0x0602), 1)


class TestWFI(unittest.TestCase):

    WFI_PROGRAM = """
            LDIA HANDLER
            OUTP 0x38
            STI
            LDIA 0x0300
            OUTP 0x30
            TRAPA
            WFI
            LDA  FLAG
            STA  OUT
            HLT
    HANDLER: PUSH
            LDIA 7
            STA  FLAG
            POP
            IRET
    .DATA
    .ORG 0x0600
    FLAG:   .DW 0
    OUT:    .DW 0
    """

    def test_wfi_parks_until_the_reply_arrives(self):
        machine = build(self.WFI_PROGRAM)
        machine.run()                      # ASYNC at TRAPA
        parked = machine.run()             # executes WFI
        self.assertEqual(parked.state, State.TRAPPED)

        final = machine.resume(
            render_reply(machine.async_pending.nonce, ["1"], checksum="crc")
        )
        self.assertEqual(final.state, State.HALTED)
        self.assertEqual(machine.read16(0x0602), 7)

    def test_wfi_with_no_interrupt_source_faults_rather_than_hangs(self):
        machine = Machine(seed=1)
        machine.load(assemble("WFI\nHLT\n"))
        result = machine.run()
        self.assertEqual(result.state, State.FAULT)
        self.assertIn("no interrupt source", result.reason)

    def test_wfi_with_a_masked_pending_irq_is_a_deadlock_fault(self):
        """WFI parks for an interrupt; if the only pending one is masked, no
        event can ever wake the machine, so it faults instead of hanging."""
        machine = Machine(seed=1)
        machine.load(assemble("CLI\nWFI\nHLT\n"))
        machine.irq_pending = True          # pending, but CLI masks it
        result = machine.run()
        self.assertEqual(result.state, State.FAULT)
        self.assertIn("masked", result.reason)


class TestInterruptMasking(unittest.TestCase):

    def test_a_masked_pending_irq_does_not_fire(self):
        machine = Machine(seed=1)
        machine.load(assemble(
            "LDIA HANDLER\nOUTP 0x38\nCLI\nNOP\nNOP\nHLT\n"
            "HANDLER: LDIA 0xAA\nSTA 0x0600\nIRET\n"
        ))
        machine.irq_pending = True          # pending before the program runs
        machine.run()                       # program masks with CLI and halts
        self.assertTrue(machine.irq_pending)          # still pending
        self.assertEqual(machine.read16(0x0600), 0)   # handler never ran

    def test_sti_delivers_a_pending_irq_at_the_next_boundary(self):
        machine = Machine(seed=1)
        machine.load(assemble(
            "LDIA HANDLER\nOUTP 0x38\nSTI\nNOP\nHLT\n"
            "HANDLER: PUSH\nLDIA 0xAA\nSTA 0x0600\nPOP\nIRET\n"
        ))
        machine.irq_pending = True
        machine.run()                       # STI unmasks; the IRQ fires
        self.assertEqual(machine.read16(0x0600), 0xAA)
        self.assertFalse(machine.irq_pending)

    def test_a_synchronous_trap_while_async_is_in_flight_is_busy(self):
        source = """
                LDIA 0x0300
                OUTP 0x30
                TRAPA
                LDIA 0x0300
                OUTP 0x30
                TRAP
                HLT
        """
        machine = build(source)
        machine.run()                      # ASYNC
        machine.run()                      # hits the second (sync) TRAP
        self.assertEqual(machine.A, Status.BUSY)


class TestSnapshotAcrossAsync(unittest.TestCase):

    def test_a_parked_async_machine_round_trips(self):
        from trapcpu.snapshot import dump, mount

        machine = build(TestWFI.WFI_PROGRAM)
        machine.run()
        machine.run()                      # parked at WFI
        self.assertEqual(machine.state, State.TRAPPED)

        restored, report = mount(dump(machine))
        self.assertTrue(report.clean)
        self.assertEqual(restored.state, State.TRAPPED)
        self.assertEqual(restored.async_pending.channel, "ASYNC")

        final = restored.resume(
            render_reply(restored.async_pending.nonce, ["1"], checksum="crc")
        )
        self.assertEqual(final.state, State.HALTED)
        self.assertEqual(restored.read16(0x0602), 7)

    def test_interrupt_state_survives_a_snapshot(self):
        from trapcpu.snapshot import dump, mount

        machine = Machine(seed=1)
        machine.load(assemble("LDIA 0x1234\nOUTP 0x38\nSTI\nHLT\n"))
        machine.run()
        restored, _ = mount(dump(machine))
        self.assertEqual(restored.ivec, 0x1234)
        self.assertEqual(restored.ien, 1)


if __name__ == "__main__":
    unittest.main()
