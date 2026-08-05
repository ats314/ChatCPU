"""TRAPCPU: a 16-bit computer with a language model on the motherboard.

ChatCPU built a computer inside a model's sandbox. TRAPCPU inverts the
relationship: the model becomes a memory-mapped coprocessor at I/O port 0x30,
and a program invokes it with an instruction.

    LDIA request        ; descriptor address
    OUTP 0x30           ; latch it into the oracle controller
    TRAP                ; the machine stops here until the model answers

Quick start::

    from trapcpu import Machine, assemble, EchoOracle

    machine = Machine()
    machine.load(assemble(open("programs/oracle_hello.asm").read()))
    machine.execute(EchoOracle())
    print(machine.output())
"""

from .assembler import AssemblyError, Program, assemble, assemble_file
from .isa import OPS, PORT_ORACLE, PORT_ORACLE_STAT
from .machine import (
    Machine,
    MachineError,
    OracleStats,
    PendingTrap,
    RunResult,
    State,
)
from .oracle import (
    ALL_FAULTS,
    CallbackOracle,
    EchoOracle,
    Fault,
    FaultInjector,
    ManualOracle,
    NoisyOracle,
    Oracle,
    OracleExhausted,
    ScriptedOracle,
    TracingOracle,
)
from .protocol import (
    DESCRIPTOR_SIZE,
    PROTOCOL_VERSION,
    Flag,
    Mode,
    OracleResult,
    Status,
    TrapFrame,
    crc16,
    pack_descriptor,
    render_reply,
    status_name,
    validate,
)
from .llm import ClaudeOracle, OracleTransportError
from .snapshot import MountReport, SnapshotError, dump, mount

__version__ = "1.0.0"

__all__ = [
    "ALL_FAULTS", "AssemblyError", "CallbackOracle", "ClaudeOracle",
    "DESCRIPTOR_SIZE",
    "EchoOracle", "Fault", "FaultInjector", "Flag", "Machine", "MachineError",
    "ManualOracle", "Mode", "MountReport", "OPS", "Oracle", "OracleExhausted",
    "OracleResult", "OracleStats", "OracleTransportError", "PORT_ORACLE",
    "PORT_ORACLE_STAT",
    "PROTOCOL_VERSION", "PendingTrap", "Program", "RunResult",
    "NoisyOracle", "ScriptedOracle", "SnapshotError", "State", "Status", "TracingOracle",
    "TrapFrame", "__version__", "assemble", "assemble_file", "crc16", "dump",
    "mount",
    "pack_descriptor", "render_reply", "status_name", "validate",
]
