"""TRAPCPU instruction set.

TRAPCPU is a fork of ChatCPU. The base ISA (0x00-0x21) is unchanged so
existing ChatCPU programs still assemble and run. Everything from 0x22 up is
new, and exists for one reason: to make it practical to write assembly that
builds a prompt in RAM, hands it to the oracle coprocessor, and walks the
reply back out again.

Register conventions used throughout the machine:

    A   accumulator / primary operand / syscall status
    B   pointer register (all indirect access goes through B)
    C   counter
    D   scratch, and the destination for DIV remainders and NUM oracle results
"""

# ---------------------------------------------------------------------------
# I/O PORTS
# ---------------------------------------------------------------------------

PORT_KEY = 0x00        # read: pop a keycode off the input queue (0 if empty)
PORT_KEY_STATE = 0x01  # read: 1 if a keycode is queued
PORT_SCREEN = 0x10     # write: plot chr(A) at (B, C)
PORT_RANDOM = 0x20     # read: uniform byte
PORT_ORACLE = 0x30     # write: latch A as the oracle request descriptor pointer.
                       # Ports 0x30-0x37 all latch; the low three bits select
                       # the DEVICE the frame is addressed to (0x30 = device 0).
PORT_ORACLE_END = 0x37
PORT_ORACLE_STAT = 0x31  # read: status of the most recent completed trap
PORT_IVEC = 0x38       # write: interrupt vector address; read: async channel
                       # state (0 idle, 1 in flight, 2 completed awaiting IRQ)
PORT_MPROT = 0x39      # write: A = (page << 8) | flag; flag 1 marks the 256
                       # byte RAM page executable (and therefore unwritable)

# ---------------------------------------------------------------------------
# OPCODES
# ---------------------------------------------------------------------------

OPS = {
    # --- base ChatCPU ISA -------------------------------------------------
    "NOP": 0x00,

    "LDIA": 0x01,   # A <- imm16
    "LDIB": 0x02,   # B <- imm16
    "LDIC": 0x03,   # C <- imm16
    "LDID": 0x04,   # D <- imm16

    "ADD": 0x05,    # A <- A + B
    "SUB": 0x06,    # A <- A - B
    "INC": 0x07,    # A <- A + 1
    "DEC": 0x08,    # A <- A - 1

    "STA": 0x09,    # RAM16[imm16] <- A
    "LDA": 0x0A,    # A <- RAM16[imm16]

    "JMP": 0x0B,
    "JZ": 0x0C,
    "JNZ": 0x0D,

    "CMP": 0x0E,    # flags from A - B, A unchanged

    "PUSH": 0x0F,   # push A
    "POP": 0x10,    # pop A

    "CALL": 0x11,
    "RET": 0x12,

    "OUT": 0x13,    # emit chr(A & 0xFF) to the console
    "IN": 0x14,     # A <- 0 (legacy no-op input)

    "HLT": 0x15,

    "MOVAB": 0x16,  # A <- B
    "MOVBA": 0x17,  # B <- A

    "ADDC": 0x18,   # A <- A + C
    "ADDD": 0x19,   # A <- A + D
    "SUBC": 0x1A,   # A <- A - C
    "SUBD": 0x1B,   # A <- A - D

    "XOR": 0x1C,
    "AND": 0x1D,
    "OR": 0x1E,
    "NOT": 0x1F,

    "INP": 0x20,    # A <- port[imm8]
    "OUTP": 0x21,   # port[imm8] <- A

    # --- TRAPCPU additions ------------------------------------------------
    "TRAP": 0x22,   # invoke the oracle coprocessor; suspends the machine

    "STB": 0x23,    # RAM8[B]  <- A & 0xFF
    "LDB": 0x24,    # A        <- RAM8[B]        (zero extended)
    "STW": 0x25,    # RAM16[B] <- A
    "LDW": 0x26,    # A        <- RAM16[B]

    "INCB": 0x27,
    "DECB": 0x28,
    "INCC": 0x29,
    "DECC": 0x2A,

    "MOVAC": 0x2B,  # A <- C
    "MOVCA": 0x2C,  # C <- A
    "MOVAD": 0x2D,  # A <- D
    "MOVDA": 0x2E,  # D <- A

    "JN": 0x2F,     # jump if N
    "JNN": 0x30,    # jump if not N
    "JC": 0x31,     # jump if CF
    "JNC": 0x32,    # jump if not CF

    "MUL": 0x33,    # A <- (A * B) & 0xFFFF, CF on overflow
    "DIV": 0x34,    # A <- A // B, D <- A % B; CF=1 and A unchanged if B == 0
    "SHL": 0x35,    # A <- A << 1, CF <- bit shifted out
    "SHR": 0x36,    # A <- A >> 1, CF <- bit shifted out

    "OUTS": 0x37,   # emit the NUL terminated string at RAM[B] to the console
    "CMPC": 0x38,   # flags from A - C, A unchanged

    # --- phase 3: interrupts and executable RAM ---------------------------
    "TRAPA": 0x39,  # asynchronous trap: publish the request and keep running;
                    # completion arrives as an interrupt
    "WFI": 0x3A,    # wait for interrupt (parks the machine if none can come)
    "CLI": 0x3B,    # mask interrupts
    "STI": 0x3C,    # unmask interrupts
    "IRET": 0x3D,   # return from interrupt: pops PC then CS, unmasks
    "CALLX": 0x3E,  # call executable RAM: pushes CS then PC, fetches from RAM
    "RETX": 0x3F,   # return from CALLX: pops PC then CS

    # --- phase 4: indexed addressing --------------------------------------
    # B is the base of an array, C is an element index. The scale is the
    # element width, so C counts elements rather than bytes and the caller
    # never open codes a multiply.
    "LDWX": 0x40,   # A        <- RAM16[B + C*2]
    "STWX": 0x41,   # RAM16[B + C*2] <- A
    "LDBX": 0x42,   # A        <- RAM8[B + C]     (zero extended)
    "STBX": 0x43,   # RAM8[B + C]    <- A & 0xFF
}

# Mnemonics that carry a one byte operand.
ARG8_OPS = frozenset({"INP", "OUTP"})

# Mnemonics that carry a little endian two byte operand.
ARG16_OPS = frozenset({
    "LDIA", "LDIB", "LDIC", "LDID",
    "STA", "LDA",
    "JMP", "JZ", "JNZ",
    "CALL", "CALLX",
    "JN", "JNN", "JC", "JNC",
})

MNEMONICS = {opcode: name for name, opcode in OPS.items()}


def width(mnemonic):
    """Encoded size in bytes of one instruction."""
    mnemonic = mnemonic.upper()
    if mnemonic in ARG8_OPS:
        return 2
    if mnemonic in ARG16_OPS:
        return 3
    return 1
