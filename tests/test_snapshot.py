import random
import unittest

from trapcpu import Machine, State, assemble
from trapcpu.oracle import EchoOracle
from trapcpu.protocol import DESCRIPTOR_SIZE, pack_descriptor, render_reply
from trapcpu.snapshot import (
    SnapshotError,
    dump,
    find_snapshots,
    mount,
    select,
    simulate_bitrot,
    simulate_eviction,
)

DESC = 0x0300
PROMPT = 0x0400
BUFFER = 0x0500

TRAP_PROGRAM = """
        LDIA 0x0300
        OUTP 0x30
        TRAP
        LDIB 0x0500
        OUTS
        HLT
"""


def trapping_machine(seed=11, prompt="Name a colour. [hint: BLUE]"):
    machine = Machine(seed=seed)
    machine.load(assemble(TRAP_PROGRAM))
    machine.name = "colour"
    machine.ram[DESC:DESC + DESCRIPTOR_SIZE] = pack_descriptor(PROMPT, BUFFER, 32)
    blob = prompt.encode("utf-8")
    machine.ram[PROMPT:PROMPT + len(blob)] = blob
    return machine


def halted_machine():
    machine = trapping_machine()
    machine.execute(EchoOracle())
    return machine


class TestRoundTrip(unittest.TestCase):

    def test_a_halted_machine_survives(self):
        original = halted_machine()
        restored, report = mount(dump(original))
        self.assertTrue(report.clean)
        self.assertEqual(restored.ram, original.ram)
        self.assertEqual(restored.rom, original.rom)
        self.assertEqual(restored.PC, original.PC)
        self.assertEqual(restored.A, original.A)
        self.assertEqual(restored.cycles, original.cycles)

    def test_statistics_survive(self):
        original = halted_machine()
        restored, _ = mount(dump(original))
        self.assertEqual(restored.stats.traps, original.stats.traps)
        self.assertEqual(restored.stats.ok, original.stats.ok)
        self.assertEqual(restored.traps_used, original.traps_used)

    def test_the_snapshot_can_be_buried_in_a_conversation(self):
        text = (
            "Sure, here is the machine state:\n\n"
            + dump(halted_machine())
            + "\n\nLet me know if you want me to continue!"
        )
        _, report = mount(text)
        self.assertTrue(report.clean)

    def test_generation_increments(self):
        machine = halted_machine()
        first, _ = mount(dump(machine))
        self.assertEqual(first.generation, 1)
        second, _ = mount(dump(first))
        self.assertEqual(second.generation, 2)

    def test_the_newest_generation_wins(self):
        machine = halted_machine()
        old = dump(machine, generation=1)
        new = dump(machine, generation=7)
        chosen, _ = select(old + "\n" + new)
        self.assertEqual(chosen.generation, 7)
        chosen, _ = select(new + "\n" + old)
        self.assertEqual(chosen.generation, 7)

    def test_no_snapshot_at_all(self):
        with self.assertRaises(SnapshotError):
            mount("just some chat, no frames here")

    def test_an_unterminated_snapshot_is_not_a_candidate(self):
        text = dump(halted_machine()).replace("=== TRAPCPU END ===", "")
        with self.assertRaises(SnapshotError):
            mount(text)


class TestPendingTrap(unittest.TestCase):
    """A machine parked mid-trap must survive a context reset."""

    def test_a_trapped_machine_round_trips(self):
        machine = trapping_machine()
        result = machine.run()
        nonce = result.frame.nonce

        restored, report = mount(dump(machine))
        self.assertEqual(restored.state, State.TRAPPED)
        self.assertEqual(restored.pending.nonce, nonce)
        self.assertIn("mid-trap", " ".join(report.notes))

    def test_the_restored_machine_accepts_the_original_reply(self):
        machine = trapping_machine()
        result = machine.run()
        reply = render_reply(result.frame.nonce, ["TEAL"], checksum="crc")

        restored, _ = mount(dump(machine))
        final = restored.resume(reply)
        self.assertEqual(final.state, State.HALTED)
        self.assertEqual(restored.output(), "TEAL")

    def test_the_prompt_survives_so_a_retry_can_be_reissued(self):
        machine = trapping_machine()
        machine.run()
        restored, _ = mount(dump(machine))
        frame = restored.pending.to_frame(restored)
        self.assertIn("Name a colour", frame.prompt)

    def test_retry_history_survives(self):
        machine = trapping_machine()
        machine.load(assemble(TRAP_PROGRAM))
        machine.ram[DESC:DESC + DESCRIPTOR_SIZE] = pack_descriptor(
            PROMPT, BUFFER, 32, retries=3
        )
        machine.ram[PROMPT:PROMPT + 5] = b"hi?\x00\x00"
        machine.run()
        machine.resume("nonsense")

        restored, _ = mount(dump(machine))
        self.assertEqual(restored.pending.attempt, 2)
        self.assertEqual(len(restored.pending.history), 1)


class TestBitRot(unittest.TestCase):

    def test_a_mangled_sector_is_named_not_silently_restored(self):
        rotted = simulate_bitrot(dump(halted_machine()), flips=1,
                                 rng=random.Random(2))
        machine, report = mount(rotted)
        self.assertFalse(report.clean)
        self.assertEqual(len(report.bad_sectors), 1)
        self.assertEqual(report.bytes_lost, 32)
        self.assertIn("CRC mismatch", report.bad_sectors[0].reason)

    def test_a_rotted_sector_reads_as_zeros_rather_than_garbage(self):
        original = halted_machine()
        rotted = simulate_bitrot(dump(original), flips=1, rng=random.Random(2))
        machine, report = mount(rotted)
        lost = report.bad_sectors[0]
        window = slice(lost.address, lost.address + lost.length)
        memory = machine.rom if lost.space == "rom" else machine.ram
        self.assertEqual(bytes(memory[window]), b"\x00" * lost.length)

    def test_an_evicted_sector_is_detected_by_the_allocation_map(self):
        evicted = simulate_eviction(dump(halted_machine()), fraction=0.5,
                                    rng=random.Random(3))
        _, report = mount(evicted)
        self.assertTrue(report.bad_sectors)
        self.assertTrue(
            any("evicted" in sector.reason for sector in report.bad_sectors)
        )

    def test_eviction_without_the_map_would_be_silent(self):
        """The map is what makes loss observable; prove it by removing it."""
        text = dump(halted_machine())
        without_map = "\n".join(
            line for line in text.split("\n") if not line.startswith("RAMMAP")
        )
        evicted = simulate_eviction(without_map, fraction=0.5,
                                    rng=random.Random(3))
        _, report = mount(evicted)
        self.assertFalse(
            any("evicted" in sector.reason for sector in report.bad_sectors)
        )

    def test_losing_the_register_line_makes_the_frame_unmountable(self):
        text = "\n".join(
            line for line in dump(halted_machine()).split("\n")
            if not line.startswith("REGS")
        )
        with self.assertRaises(SnapshotError):
            mount(text)

    def test_an_older_intact_generation_is_a_usable_fallback(self):
        machine = halted_machine()
        good = dump(machine, generation=1)
        broken = simulate_bitrot(dump(machine, generation=2), flips=3,
                                 rng=random.Random(5))
        transcript = good + "\n" + broken

        # select() prefers the newest, which is the damaged one.
        newest, _ = select(transcript)
        self.assertEqual(newest.generation, 2)
        self.assertTrue(newest.bad_sectors)

        # The intact older frame is still there to fall back to.
        older = [s for s in find_snapshots(transcript) if s.generation == 1][0]
        self.assertFalse(older.bad_sectors)

    def test_snapcrc_catches_header_tampering(self):
        text = dump(halted_machine()).replace("CYCLE:", "CYCLE:  ")
        _, report = mount(text)
        self.assertFalse(report.snapcrc_ok)


class TestSectorEncoding(unittest.TestCase):

    def test_zero_regions_are_not_written_out(self):
        machine = halted_machine()
        text = dump(machine)
        addresses = [
            line.split()[0] for line in text.split("\n") if line.startswith("@")
        ]
        self.assertTrue(addresses)
        self.assertLess(len(text), 4096, "64 KiB of zeros must not be emitted")

    def test_every_sector_line_carries_its_own_checksum(self):
        for line in dump(halted_machine()).split("\n"):
            if line.startswith("@"):
                parts = line.split()
                self.assertEqual(len(parts), 3)
                self.assertEqual(len(parts[2]), 4)

    def test_rom_can_be_omitted_for_a_smaller_disk(self):
        machine = halted_machine()
        self.assertLess(len(dump(machine, include_rom=False)), len(dump(machine)))


if __name__ == "__main__":
    unittest.main()
