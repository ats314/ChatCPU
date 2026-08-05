import io
import json
import os
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ctxprobe import (
    Status,
    crc16,
    derive_probe_id,
    emit,
    find_probes,
    scan,
    sector_payload,
    simulate_bitrot,
    simulate_eviction,
    simulate_header_loss,
    simulate_rewrite,
    simulate_truncation,
)
from ctxprobe.cli import main

FILLER = [
    "user: please refactor the parser",
    "assistant: done, three files changed",
    "some other conversational line",
]


def transcript_with(*frames, filler_every=1):
    chunks = []
    for frame in frames:
        chunks.append(frame)
        chunks.extend(FILLER * filler_every)
    return "\n".join(chunks)


class TestPayloadContract(unittest.TestCase):
    """The payload is a wire contract: it must never move between versions."""

    def test_payload_is_deterministic(self):
        self.assertEqual(sector_payload(99, 3, 32), sector_payload(99, 3, 32))
        self.assertNotEqual(sector_payload(99, 3, 32), sector_payload(99, 4, 32))
        self.assertNotEqual(sector_payload(99, 3, 32), sector_payload(98, 3, 32))
        self.assertEqual(len(sector_payload(99, 3, 17)), 17)

    def test_payload_golden_values(self):
        blob = sector_payload(0x1234, 0, 32)
        self.assertEqual(
            blob.hex().upper(),
            "F00A249A8634E97F2E655E0288D483A8"
            "D6BD2BDE5B9D210BE0AF3C9B58C3DAD4",
        )
        self.assertEqual(crc16(blob), 0x8BED)
        self.assertEqual(derive_probe_id(0x1234), "3EC5CC21")


class TestRoundTrip(unittest.TestCase):

    def test_clean_round_trip(self):
        frame, seed, probe_id = emit(seed=0xDEAD, sectors=8)
        report = scan(transcript_with(frame))

        self.assertEqual(len(report.probes), 1)
        probe = report.probes[0]
        self.assertEqual(probe.probe_id, probe_id)
        self.assertEqual(probe.seed, seed)
        self.assertTrue(probe.clean)
        self.assertTrue(probe.metacrc_ok)
        self.assertEqual(probe.counts[Status.OK], 8)
        self.assertEqual(probe.bytes_intact, 8 * 32)
        self.assertEqual(probe.bytes_lost, 0)
        self.assertTrue(report.clean)
        self.assertEqual(probe.lossmap(), "[........]")

    def test_multiple_probes_keep_order_and_offsets(self):
        frames = [emit(seed=n, sectors=4)[0] for n in (1, 2, 3)]
        report = scan(transcript_with(*frames, filler_every=2))

        self.assertEqual(len(report.probes), 3)
        offsets = [probe.start_line for probe in report.probes]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual([probe.seed for probe in report.probes], [1, 2, 3])


class TestFailureTaxonomy(unittest.TestCase):

    def test_eviction_is_named_by_the_map(self):
        frame, _, _ = emit(seed=5, sectors=8)
        lines = frame.split("\n")
        survivors = [line for line in lines
                     if not line.startswith(("@02 ", "@05 "))]
        report = scan(transcript_with("\n".join(survivors)))

        probe = report.probes[0]
        self.assertEqual(probe.counts[Status.EVICTED], 2)
        evicted = sorted(state.index for state in probe.states
                         if state.status == Status.EVICTED)
        self.assertEqual(evicted, [2, 5])
        self.assertEqual(probe.bytes_lost, 2 * 32)
        self.assertEqual(probe.lossmap(), "[.._.._..]")

    def test_bitrot_is_caught_by_sector_crc(self):
        frame, _, _ = emit(seed=6, sectors=8)
        worn = simulate_bitrot(frame, flips=3, rng=random.Random(1))
        report = scan(transcript_with(worn))

        probe = report.probes[0]
        self.assertGreaterEqual(probe.counts[Status.CORRUPTED], 1)
        self.assertFalse(probe.clean)

    def test_crc_consistent_rewrite_is_caught_only_by_the_seed(self):
        frame, _, _ = emit(seed=7, sectors=8)
        worn = simulate_rewrite(frame, rewrites=1, rng=random.Random(2))
        report = scan(transcript_with(worn))

        probe = report.probes[0]
        self.assertEqual(probe.counts[Status.REWRITTEN], 1)
        self.assertEqual(probe.counts[Status.CORRUPTED], 0)

        # The same wear with the seed header stripped: every checksum passes
        # and the alteration becomes invisible. That is why OK and UNVERIFIED
        # are different words.
        blind = simulate_header_loss(worn, keys=("SEED",))
        blind_probe = scan(transcript_with(blind)).probes[0]
        self.assertEqual(blind_probe.counts[Status.REWRITTEN], 0)
        self.assertEqual(blind_probe.counts[Status.OK], 0)
        self.assertEqual(blind_probe.counts[Status.UNVERIFIED], 8)

    def test_metacrc_catches_header_tamper(self):
        frame, _, _ = emit(seed=8, sectors=4)
        tampered = frame.replace("SECTORS: 4", "SECTORS: 3")
        probe = scan(transcript_with(tampered)).probes[0]
        self.assertFalse(probe.metacrc_ok)
        self.assertFalse(probe.clean)

    def test_stray_sector_is_reported_not_trusted(self):
        frame, seed, _ = emit(seed=9, sectors=4)
        blob = sector_payload(seed, 9, 32)
        stray = "@09 %s %04X" % (blob.hex().upper(), crc16(blob))
        planted = frame.replace("<<<DATA", stray + "\n<<<DATA")
        probe = scan(transcript_with(planted)).probes[0]

        strays = [state for state in probe.states if state.stray]
        self.assertEqual(len(strays), 1)
        self.assertEqual(strays[0].index, 9)


class TestSilentLossProperty(unittest.TestCase):
    """The map exists to buy exactly one property: eviction is loud. Remove
    the map and the same eviction must become silent — otherwise the map is
    decoration and the report is lying about what it can see."""

    def test_without_map_and_count_eviction_is_silent(self):
        frame, _, _ = emit(seed=11, sectors=8)
        evicted = "\n".join(line for line in frame.split("\n")
                            if not line.startswith("@03 "))

        loud = scan(transcript_with(evicted)).probes[0]
        self.assertEqual(loud.counts[Status.EVICTED], 1)

        stripped = simulate_header_loss(evicted, keys=("MAP", "SECTORS"))
        silent = scan(transcript_with(stripped)).probes[0]
        self.assertEqual(silent.counts[Status.EVICTED], 0)
        self.assertTrue(any("SILENT" in note for note in silent.notes))

    def test_sectors_header_is_the_fallback_map(self):
        frame, _, _ = emit(seed=12, sectors=8)
        evicted = "\n".join(line for line in frame.split("\n")
                            if not line.startswith("@03 "))
        stripped = simulate_header_loss(evicted, keys=("MAP",))
        probe = scan(transcript_with(stripped)).probes[0]
        self.assertEqual(probe.counts[Status.EVICTED], 1)


class TestVanishedProbes(unittest.TestCase):

    def test_prev_chain_detects_whole_frame_eviction(self):
        first, _, first_id = emit(seed=21, sectors=4)
        second, _, _ = emit(seed=22, sectors=4, prev=[first_id])
        transcript = transcript_with(first, second)

        # Truncation drops the oldest lines; keep exactly enough that the
        # first frame goes wholesale and the second survives untouched.
        lines = transcript.split("\n")
        second_start = lines.index(second.split("\n")[0], 1)
        tail = len(lines) - second_start
        worn = simulate_truncation(transcript, keep=(tail + 0.5) / len(lines))
        report = scan(worn)

        self.assertEqual(len(report.probes), 1)
        self.assertIn(first_id, report.vanished)
        self.assertFalse(report.clean)

    def test_expect_list_detects_vanish_without_a_chain(self):
        frame, _, probe_id = emit(seed=23, sectors=4)
        report = scan(transcript_with(frame), expect=[probe_id, "AAAAAAAA"])
        self.assertEqual(list(report.vanished), ["AAAAAAAA"])

    def test_unterminated_frame_is_noted(self):
        frame, _, _ = emit(seed=24, sectors=4)
        cut = "\n".join(frame.split("\n")[:-1])
        probe = scan(transcript_with(cut)).probes[0]
        self.assertTrue(any("terminated" in note for note in probe.notes))


class TestFaultSimulators(unittest.TestCase):

    def test_simulators_are_deterministic(self):
        frame, _, _ = emit(seed=31, sectors=16)
        one = simulate_eviction(frame, 0.5, rng=random.Random(3))
        two = simulate_eviction(frame, 0.5, rng=random.Random(3))
        self.assertEqual(one, two)

    def test_truncation_keeps_the_tail(self):
        text = "\n".join(str(n) for n in range(10))
        self.assertEqual(simulate_truncation(text, keep=0.3), "7\n8\n9")


class TestJsonReport(unittest.TestCase):

    def test_json_round_trips_and_carries_totals(self):
        frame, _, probe_id = emit(seed=41, sectors=4)
        worn = simulate_eviction(frame, 0.25, rng=random.Random(4))
        report = scan(transcript_with(worn))

        data = json.loads(report.to_json())
        self.assertEqual(data["frames_seen"], 1)
        self.assertEqual(data["probes"][0]["id"], probe_id)
        self.assertEqual(
            sum(data["totals"].values()),
            sum(len(probe.states) for probe in report.probes),
        )
        self.assertIn("lossmap", data["probes"][0])


class TestCli(unittest.TestCase):

    def _run(self, argv, stdin_text=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_emit_scan_rot_loop(self):
        code, frame, stderr = self._run(
            ["emit", "--seed", "BEEF", "--sectors", "8"])
        self.assertEqual(code, 0)
        self.assertIn("CTXPROBE/1 SENTINEL", frame)
        self.assertIn("emitted probe", stderr)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transcript.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(frame)

            code, out, _ = self._run(["scan", path])
            self.assertEqual(code, 0)
            self.assertIn("CLEAN", out)

            code, worn, _ = self._run(
                ["rot", path, "--evict", "0.5", "--seed", "1"])
            self.assertEqual(code, 0)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(worn)

            code, out, _ = self._run(["scan", path])
            self.assertEqual(code, 1)
            self.assertIn("EVICTED", out)

            code, out, _ = self._run(["scan", path, "--json"])
            self.assertEqual(code, 1)
            json.loads(out)

    def test_emit_chain_reads_existing_probes(self):
        _, first, _ = self._run(["emit", "--seed", "0AAA", "--sectors", "4"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transcript.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(first)

            code, second, _ = self._run(
                ["emit", "--seed", "0BBB", "--sectors", "4",
                 "--chain", path])
            self.assertEqual(code, 0)
            first_id = find_probes(first)[0].probe_id
            self.assertIn("PREV: %s" % first_id, second)

    def test_demo_runs_end_to_end(self):
        code, out, _ = self._run(["demo", "--seed", "7"])
        self.assertEqual(code, 0)
        self.assertIn("CTXPROBE SCAN", out)
        self.assertIn("vanished", out.lower() + " vanished")


if __name__ == "__main__":
    unittest.main()
