"""The pasteable bundle is a shipped artifact, so it is tested like one."""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(HERE, "bootstrap.py")
BUNDLER = os.path.join(HERE, "tools", "bundle.py")


class TestBundle(unittest.TestCase):

    def test_the_bundle_is_in_sync_with_the_package(self):
        result = subprocess.run(
            [sys.executable, BUNDLER, "--check"],
            capture_output=True, text=True, cwd=HERE,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_it_has_no_third_party_or_relative_imports(self):
        with open(BUNDLE, encoding="utf-8") as handle:
            source = handle.read()

        allowed = {"argparse", "collections", "io", "json", "os", "random", "re",
                   "sys", "time", "urllib"}
        for lineno, line in enumerate(source.split("\n"), start=1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            self.assertFalse(
                stripped.startswith("from ."),
                f"line {lineno} still imports from the package: {stripped}",
            )
            module = stripped.split()[1].split(".")[0]
            self.assertIn(module, allowed, f"line {lineno}: {stripped}")

    def test_it_executes_standalone_and_exposes_the_api(self):
        namespace = {}
        with open(BUNDLE, encoding="utf-8") as handle:
            exec(compile(handle.read(), BUNDLE, "exec"), namespace)

        for name in ("Machine", "assemble", "EchoOracle", "dump", "mount",
                     "validate", "Status", "TrapFrame", "demo", "main"):
            self.assertIn(name, namespace)

    def test_the_bundled_machine_completes_a_trap(self):
        namespace = {}
        with open(BUNDLE, encoding="utf-8") as handle:
            exec(compile(handle.read(), BUNDLE, "exec"), namespace)

        machine = namespace["Machine"](seed=1)
        machine.load(namespace["assemble"](namespace["DEMO"]))
        result = machine.run()
        self.assertTrue(result.trapped)

        result = machine.resume(namespace["EchoOracle"]().ask(result.frame))
        self.assertEqual(result.state, "HALTED")
        self.assertIn("HELLO FROM THE OTHER SIDE", machine.output())

    def test_the_bundled_snapshot_layer_round_trips(self):
        namespace = {}
        with open(BUNDLE, encoding="utf-8") as handle:
            exec(compile(handle.read(), BUNDLE, "exec"), namespace)

        machine = namespace["Machine"](seed=1)
        machine.load(namespace["assemble"](namespace["DEMO"]))
        machine.execute(namespace["EchoOracle"]())

        restored, report = namespace["mount"](namespace["dump"](machine))
        self.assertTrue(report.clean)
        self.assertEqual(restored.ram, machine.ram)

    def test_it_runs_as_a_script(self):
        result = subprocess.run(
            [sys.executable, BUNDLE], capture_output=True, text=True, cwd=HERE
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TRAPCPU", result.stdout)

    def test_it_runs_as_a_cli(self):
        result = subprocess.run(
            [sys.executable, BUNDLE, "isa"],
            capture_output=True, text=True, cwd=HERE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TRAP", result.stdout)


if __name__ == "__main__":
    unittest.main()
