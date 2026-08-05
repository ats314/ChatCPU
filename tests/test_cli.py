import io
import os
import tempfile
import unittest

from trapcpu.cli import build_oracle, main
from trapcpu.protocol import render_reply
from trapcpu.snapshot import find_snapshots

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRAMS = os.path.join(HERE, "programs", "trap")


def invoke(*argv):
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


class TestRun(unittest.TestCase):

    def test_hello_with_the_echo_oracle(self):
        code, text = invoke(
            "run", os.path.join(PROGRAMS, "oracle_hello.asm"), "--oracle", "echo"
        )
        self.assertEqual(code, 0)
        self.assertIn("HELLO FROM THE OTHER SIDE OF THE BUS", text)

    def test_stats_are_printed_on_request(self):
        _, text = invoke(
            "run", os.path.join(PROGRAMS, "oracle_hello.asm"), "--stats"
        )
        self.assertIn("traps issued", text)

    def test_guess_with_the_bisect_oracle(self):
        _, text = invoke(
            "run", os.path.join(PROGRAMS, "oracle_guess.asm"),
            "--oracle", "bisect", "--seed", "4",
        )
        self.assertIn("correct!", text)

    def test_fault_injection_from_the_command_line(self):
        _, text = invoke(
            "run", os.path.join(PROGRAMS, "oracle_hello.asm"),
            "--fault", "stale_nonce,bad_length", "--stats",
        )
        self.assertIn("oracle says", text)
        self.assertIn("retries consumed  : 2", text)

    def test_unknown_oracle_is_refused(self):
        with self.assertRaises(SystemExit):
            invoke("run", os.path.join(PROGRAMS, "oracle_hello.asm"),
                   "--oracle", "psychic")

    def test_unknown_fault_is_refused(self):
        with self.assertRaises(SystemExit):
            invoke("run", os.path.join(PROGRAMS, "oracle_hello.asm"),
                   "--fault", "gremlins")

    def test_a_broken_program_reports_the_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".asm", delete=False) as handle:
            handle.write("NOP\nFROB\n")
            path = handle.name
        try:
            with self.assertRaises(SystemExit) as caught:
                invoke("run", path)
            self.assertIn("FROB", str(caught.exception))
        finally:
            os.unlink(path)


class TestAsm(unittest.TestCase):

    def test_symbols_and_disassembly(self):
        code, text = invoke(
            "asm", os.path.join(PROGRAMS, "oracle_hello.asm"),
            "--symbols", "--disasm",
        )
        self.assertEqual(code, 0)
        self.assertIn("PROMPT", text)
        self.assertIn("TRAP", text)
        self.assertIn("OUTP 0x30", text)

    def test_hex_dump_by_default(self):
        _, text = invoke("asm", os.path.join(PROGRAMS, "oracle_hello.asm"))
        self.assertIn("0000:", text)


class TestConversationalLoop(unittest.TestCase):
    """frame + resume: the mode where a human ferries text to a chat window."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "disk.txt")

    def test_frame_parks_the_machine_and_prints_a_request(self):
        code, text = invoke(
            "frame", os.path.join(PROGRAMS, "oracle_guess.asm"),
            "--state", self.state, "--seed", "4",
        )
        self.assertEqual(code, 0)
        self.assertIn("ORACLE REQUEST", text)
        self.assertIn("higher or lower", text)

        with open(self.state) as handle:
            parked = handle.read()
        self.assertEqual(len(find_snapshots(parked)), 1)

    def test_resume_continues_the_program(self):
        _, text = invoke(
            "frame", os.path.join(PROGRAMS, "oracle_guess.asm"),
            "--state", self.state, "--seed", "4",
        )
        nonce = int(
            [line for line in text.split("\n") if line.startswith("NONCE:")][0]
            .split(":")[1].strip(),
            16,
        )

        reply = os.path.join(self.dir, "reply.txt")
        with open(reply, "w") as handle:
            handle.write(render_reply(nonce, ["50"], checksum="crc"))

        code, text = invoke(
            "resume", "--state", self.state, "--reply", reply
        )
        self.assertEqual(code, 0)
        self.assertIn("MOUNT CLEAN", text)
        self.assertIn("oracle guesses 50", text)
        self.assertIn("ORACLE REQUEST", text)     # the next round is queued
        self.assertIn("previous guess 50", text)

    def test_a_stale_reply_is_rejected_across_the_park(self):
        invoke("frame", os.path.join(PROGRAMS, "oracle_hello.asm"),
               "--state", self.state, "--seed", "4")
        reply = os.path.join(self.dir, "reply.txt")
        with open(reply, "w") as handle:
            handle.write(render_reply(0x0001, ["nope"], checksum="crc"))

        _, text = invoke("resume", "--state", self.state, "--reply", reply)
        self.assertIn("ORACLE REQUEST", text)  # retried, not accepted
        self.assertIn("RETRY_REASON: BAD_NONCE", text)


class TestMount(unittest.TestCase):

    def test_mount_reports_and_continues(self):
        directory = tempfile.mkdtemp()
        snapshot = os.path.join(directory, "snap.txt")
        invoke("run", os.path.join(PROGRAMS, "oracle_hello.asm"),
               "--snapshot", snapshot)

        code, text = invoke("mount", snapshot)
        self.assertEqual(code, 0)
        self.assertIn("MOUNT CLEAN", text)
        self.assertIn("STATE  = HALTED", text)

    def test_mount_of_a_rotted_transcript_reports_the_damage(self):
        from trapcpu.snapshot import simulate_bitrot

        directory = tempfile.mkdtemp()
        snapshot = os.path.join(directory, "snap.txt")
        invoke("run", os.path.join(PROGRAMS, "oracle_hello.asm"),
               "--snapshot", snapshot)

        with open(snapshot) as handle:
            text = handle.read()
        with open(snapshot, "w") as handle:
            handle.write(simulate_bitrot(text, flips=1))

        code, output = invoke("mount", snapshot)
        self.assertEqual(code, 1)
        self.assertIn("DEGRADED", output)
        self.assertIn("bad sector", output)


class TestInformational(unittest.TestCase):

    def test_isa_listing(self):
        _, text = invoke("isa")
        self.assertIn("TRAP", text)
        self.assertIn("0x22", text)

    def test_oracle_listing(self):
        _, text = invoke("oracles")
        self.assertIn("bisect", text)
        self.assertIn("stale_nonce", text)

    def test_script_oracle_reads_a_file(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "answers.txt")
        with open(path, "w") as handle:
            handle.write("MAUVE\n")
        oracle = build_oracle(f"script:{path}")
        self.assertEqual(oracle.answers, ["MAUVE"])


if __name__ == "__main__":
    unittest.main()
