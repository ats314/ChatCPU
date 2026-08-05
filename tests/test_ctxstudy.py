import io
import json
import random
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ctxprobe.cli import main
from ctxprobe.scan import Status, scan
from ctxprobe.study import (
    build_transcript,
    render_table,
    resolve_adapter,
    run_study,
    to_csv,
    to_json,
)


def study_records(**kwargs):
    defaults = dict(trials=2, probes=3, sectors=6, seed=1)
    defaults.update(kwargs)
    return run_study(**defaults)


class TestBuildTranscript(unittest.TestCase):

    def test_deterministic_and_ordered(self):
        one, ids_one = build_transcript(random.Random("x"), probes=3, sectors=4)
        two, ids_two = build_transcript(random.Random("x"), probes=3, sectors=4)
        self.assertEqual(one, two)
        self.assertEqual(ids_one, ids_two)
        self.assertEqual(len(ids_one), 3)

        # Emission order matches depth: probe 0 appears first in the text.
        report = scan(one)
        found = [probe.probe_id for probe in report.probes]
        self.assertEqual(found, ids_one)

    def test_later_probes_chain_to_earlier(self):
        text, ids = build_transcript(random.Random("y"), probes=3, sectors=4)
        report = scan(text)
        self.assertIn(ids[0], report.probes[2].prev)
        self.assertIn(ids[1], report.probes[2].prev)


class TestAdapters(unittest.TestCase):

    def test_unknown_adapter_rejected(self):
        with self.assertRaises(ValueError):
            resolve_adapter("blockchain")

    def test_cmd_adapter_runs_a_pipeline(self):
        _, adapter = resolve_adapter("cmd:cat")
        self.assertEqual(adapter("hello\nworld", random.Random(0)),
                         "hello\nworld")

    def test_cmd_adapter_surfaces_failure(self):
        _, adapter = resolve_adapter("cmd:false")
        with self.assertRaises(RuntimeError):
            adapter("text", random.Random(0))

    def test_claude_adapter_requires_credentials(self):
        import os
        from ctxprobe.llmadapter import AdapterError, compact_with_claude
        saved = {key: os.environ.pop(key, None)
                 for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            with self.assertRaises(AdapterError):
                compact_with_claude("some transcript")
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value


class TestRunStudy(unittest.TestCase):

    def test_identity_is_a_clean_control(self):
        records = study_records(adapters="identity")
        self.assertEqual(len(records), 2 * 3)
        for record in records:
            self.assertFalse(record["vanished"])
            self.assertEqual(record["survival"], 1.0)

    def test_truncation_eats_the_old_end_first(self):
        records = study_records(adapters="truncate:0.4")
        oldest = [r["survival"] for r in records if r["position"] == 0]
        newest = [r["survival"] for r in records if r["position"] == 2]
        self.assertTrue(all(s == 0.0 for s in oldest))
        self.assertTrue(all(s == 1.0 for s in newest))
        self.assertTrue(all(r["vanished"] for r in records
                            if r["position"] == 0))

    def test_reflow_registers_as_loss(self):
        records = study_records(adapters="reflow")
        self.assertTrue(any(r["survival"] < 1.0 for r in records))
        damaged = sum(r["counts"][Status.MANGLED] +
                      r["counts"][Status.EVICTED] +
                      r["counts"][Status.CORRUPTED] for r in records)
        self.assertGreater(damaged, 0)

    def test_dedup_destroys_repeated_structure_and_is_caught(self):
        # A genuine finding, preserved as a test: sector payloads are unique,
        # but the frames' structural lines (BEGIN marker, DATA>>>, SECTORS:)
        # repeat verbatim across probes, so line-dedup compaction destroys
        # every frame after the first. The instrument must report that as
        # vanished probes rather than letting it pass silently.
        records = study_records(adapters="dedup")
        first = [r for r in records if r["position"] == 0]
        later = [r for r in records if r["position"] > 0]
        self.assertTrue(all(r["survival"] == 1.0 for r in first))
        self.assertTrue(all(r["vanished"] for r in later))

    def test_study_is_deterministic(self):
        one = study_records(adapters="wear")
        two = study_records(adapters="wear")
        self.assertEqual(to_json(one), to_json(two))


class TestOutputs(unittest.TestCase):

    def test_csv_and_json_shapes(self):
        records = study_records(adapters="identity,truncate:0.4")
        csv_text = to_csv(records)
        lines = csv_text.splitlines()
        self.assertEqual(len(lines), 1 + len(records))
        self.assertTrue(lines[0].startswith("adapter,trial,position"))
        parsed = json.loads(to_json(records))
        self.assertEqual(len(parsed), len(records))
        self.assertIn("survival", parsed[0])

    def test_table_renders_every_adapter(self):
        records = study_records(adapters="identity,wear")
        table = render_table(records, probes=3)
        self.assertIn("identity", table)
        self.assertIn("wear", table)
        self.assertIn("100%", table)

    def test_cli_study_runs(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["study", "--adapters", "identity",
                         "--trials", "1", "--probes", "2",
                         "--sectors", "4", "--seed", "3"])
        self.assertEqual(code, 0)
        self.assertIn("CTXPROBE STUDY", stdout.getvalue())
        self.assertIn("100%", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
