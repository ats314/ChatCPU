import os
import tempfile
import unittest

from trapcpu.assembler import (
    AssemblyError,
    assemble,
    assemble_file,
    evaluate,
    parse_number,
    split_label,
    split_operands,
    strip_comment,
    unescape,
)


class TestLexing(unittest.TestCase):

    def test_comment_outside_quotes_is_stripped(self):
        self.assertEqual(strip_comment("LDIA 5 ; load"), "LDIA 5 ")

    def test_semicolon_inside_a_string_survives(self):
        self.assertEqual(
            strip_comment('.ASCIIZ "a;b" ; real comment'),
            '.ASCIIZ "a;b" ',
        )

    def test_semicolon_inside_a_char_literal_survives(self):
        self.assertEqual(strip_comment("LDIA ';' ; c"), "LDIA ';' ")

    def test_operands_split_on_unquoted_commas_only(self):
        self.assertEqual(
            split_operands('"a,b", 3, \'x\''),
            ['"a,b"', "3", "'x'"],
        )

    def test_label_split_ignores_colons_in_strings(self):
        self.assertEqual(split_label('X: .ASCIIZ "a:b"'), ("X", '.ASCIIZ "a:b"'))

    def test_no_label_returns_none(self):
        self.assertEqual(split_label("LDIA 5"), (None, "LDIA 5"))

    def test_escapes(self):
        self.assertEqual(unescape(r"a\nb\t\\\x41"), "a\nb\t\\A")

    def test_bad_escape_rejected(self):
        with self.assertRaises(AssemblyError):
            unescape(r"\q")


class TestNumbers(unittest.TestCase):

    def test_radixes(self):
        for text, value in [
            ("42", 42), ("0x2A", 42), ("0b101010", 42), ("0o52", 42), ("$2A", 42),
        ]:
            self.assertEqual(parse_number(text), value, text)

    def test_char_literal(self):
        self.assertEqual(parse_number("'A'"), 65)
        self.assertEqual(parse_number(r"'\n'"), 10)

    def test_symbol_is_not_a_number(self):
        self.assertIsNone(parse_number("LOOP"))

    def test_expression_arithmetic(self):
        symbols = {"BUF": 0x300, "SIZE": 16}
        self.assertEqual(evaluate("BUF+SIZE", symbols), 0x310)
        self.assertEqual(evaluate("BUF-1", symbols), 0x2FF)
        self.assertEqual(evaluate("-4", symbols), -4)

    def test_undefined_symbol(self):
        with self.assertRaises(AssemblyError):
            evaluate("NOPE", {})

    def test_circular_constant_is_caught(self):
        with self.assertRaises(AssemblyError):
            evaluate("A", {"A": "B", "B": "A"})


class TestAssembly(unittest.TestCase):

    def test_minimal_program(self):
        program = assemble("LDIA 0x1234\nHLT\n")
        self.assertEqual(program.code, bytes([0x01, 0x34, 0x12, 0x15]))

    def test_labels_resolve_to_code_addresses(self):
        program = assemble("START: LDIA 1\n JMP START\n")
        self.assertEqual(program.symbols["START"], 0)
        self.assertEqual(program.code[-2:], bytes([0x00, 0x00]))

    def test_forward_reference(self):
        program = assemble("JMP END\nNOP\nEND: HLT\n")
        self.assertEqual(program.code[1] | (program.code[2] << 8), 4)

    def test_data_section_targets_ram(self):
        program = assemble(
            '.DATA\n.ORG 0x0300\nMSG: .ASCIIZ "hi"\n.CODE\nLDIB MSG\nOUTS\nHLT\n'
        )
        self.assertEqual(program.data, [(0x0300, b"hi\x00")])
        self.assertEqual(program.symbols["MSG"], 0x0300)

    def test_db_and_dw(self):
        program = assemble(".DATA\n.ORG 0x10\n.DB 1, 'A'\n.DW 0x1234\n")
        self.assertEqual(program.data, [(0x10, bytes([1, 65, 0x34, 0x12]))])

    def test_resb_leaves_a_hole(self):
        program = assemble('.DATA\n.ORG 0\nA: .DB 1\nB: .RESB 4\nC: .DB 2\n')
        self.assertEqual(program.symbols["C"], 5)
        self.assertEqual(program.data, [(0, b"\x01"), (5, b"\x02")])

    def test_align(self):
        program = assemble(".DATA\n.ORG 1\n.ALIGN 4\nX: .DB 9\n")
        self.assertEqual(program.symbols["X"], 4)

    def test_equ_and_forward_equ(self):
        program = assemble(".EQU N, LATER\n.DATA\n.ORG 0\nLATER: .DB 7\n")
        self.assertEqual(program.symbols["N"], 0)

    def test_code_org_shifts_the_image(self):
        program = assemble(".ORG 4\nHLT\n")
        self.assertEqual(len(program.code), 5)
        self.assertEqual(program.code[4], 0x15)

    def test_duplicate_symbol_rejected(self):
        with self.assertRaises(AssemblyError):
            assemble("X: NOP\nX: NOP\n")

    def test_unknown_mnemonic_reports_the_line(self):
        with self.assertRaises(AssemblyError) as caught:
            assemble("NOP\nFROB 1\n")
        self.assertIn(":2:", str(caught.exception))
        self.assertIn("FROB", str(caught.exception))

    def test_operand_count_is_checked(self):
        with self.assertRaises(AssemblyError):
            assemble("HLT 1\n")
        with self.assertRaises(AssemblyError):
            assemble("LDIA\n")

    def test_arg8_range_is_checked(self):
        with self.assertRaises(AssemblyError):
            assemble("OUTP 0x1FF\n")

    def test_db_range_is_checked(self):
        with self.assertRaises(AssemblyError):
            assemble(".DATA\n.DB 256\n")

    def test_unterminated_string(self):
        with self.assertRaises(AssemblyError):
            assemble('.DATA\n.ASCIIZ "oops\n')


class TestIncludes(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_include_is_spliced(self):
        self.write("consts.inc", ".EQU SEVEN, 7\n")
        main = self.write("main.asm", '.INCLUDE "consts.inc"\nLDIA SEVEN\nHLT\n')
        program = assemble_file(main)
        self.assertEqual(program.code[1], 7)

    def test_include_errors_name_the_included_file(self):
        self.write("bad.inc", "NOP\nBOGUS\n")
        main = self.write("main.asm", '.INCLUDE "bad.inc"\n')
        with self.assertRaises(AssemblyError) as caught:
            assemble_file(main)
        self.assertIn("bad.inc:2", str(caught.exception))

    def test_circular_include_is_refused(self):
        self.write("a.inc", '.INCLUDE "b.inc"\n')
        self.write("b.inc", '.INCLUDE "a.inc"\n')
        main = self.write("main.asm", '.INCLUDE "a.inc"\n')
        with self.assertRaises(AssemblyError) as caught:
            assemble_file(main)
        self.assertIn("circular", str(caught.exception))

    def test_missing_include(self):
        main = self.write("main.asm", '.INCLUDE "nope.inc"\n')
        with self.assertRaises(AssemblyError):
            assemble_file(main)


if __name__ == "__main__":
    unittest.main()
