# ChatCPU

[![CI](https://github.com/ats314/ChatCPU/actions/workflows/ci.yml/badge.svg)](https://github.com/ats314/ChatCPU/actions/workflows/ci.yml)

**ChatCPU is a homemade 16 bit computer built entirely in Python and designed to run inside ChatGPT's Python code runner**

> ⚠️ **Very early access / proof of concept**

> ### 🔌 See also: [TRAPCPU](docs/TRAPCPU.md)
>
> ChatCPU hides a computer inside the model's sandbox. **TRAPCPU** turns that
> around and makes the model *a part of the computer* — a chip on the
> motherboard that programs can call, the way an old CPU called a maths
> coprocessor.
>
> Here is what that buys you. This is bubble sort — the most ordinary,
> most deterministic routine in computing — except that where it would normally
> compare two numbers, it stops the whole machine and asks:
>
> ```console
> $ python3 -m trapcpu run programs/trap/oracle_sort.asm --oracle judge
> before:                     after:
>   a house fire                a paper cut
>   a paper cut                 a wasp sting
>   a hurricane                 a car crash
>   a wasp sting                a house fire
>   a car crash                 a hurricane
>
> sorted in 16 oracle comparisons
> ```
>
> Nothing in that assembly knows a hurricane outranks a paper cut. It shuffles
> 16-bit words and asks the device which way round each pair goes. Flip the
> device's opinion and the same unmodified program sorts backwards — that is a
> [test](tests/test_sort.py), not a claim. The sort is provably correct; the
> *ordering relation* is judgement.
>
> Mechanically: a program writes a request descriptor to I/O port `0x30` and
> executes `TRAP`. The machine halts, publishes its state into the
> conversation, and the model's next reply is the hardware response — parsed,
> checksummed, voted on, and written into RAM before the next instruction runs.
>
> ```asm
> LDIA REQ        ; descriptor address
> OUTP 0x30       ; latch it into the oracle controller
> TRAP            ; the machine stops here
> ```
>
> It lives in [`trapcpu/`](trapcpu/) alongside the original and reuses the base
> ISA unchanged. The model can run asynchronously (interrupts hide its
> latency) and can even hand back **machine code** that a W^X memory system
> and a verify-then-bless pipeline let the CPU run safely. The wire format is
> specified in [`docs/TRAP_PROTOCOL.md`](docs/TRAP_PROTOCOL.md).
>
> Because everything except the model call is deterministic, you can then ask
> which of those judgements actually mattered:
>
> ```console
> $ python3 -m trapcpu ablate programs/trap/oracle_sort.asm
> CRITICALITY   load bearing : 4/16    no effect : 12/16
> SELF CORRECTION
>     first   3 random  100%  ############################
>     first   8 random   57%  #################
>     first  15 random    0%
> ```
>
> Twelve of the sixteen model calls can be individually corrupted and the output
> does not move. The first three can be literal coin flips. Bubble sort revisits
> its own decisions on every pass, so early mistakes get repaired — **the
> algorithm is error correction for the model**, and only the final pass is
> exposed. Details and prior art in [`docs/ABLATION.md`](docs/ABLATION.md).
>
> Swap `--oracle judge` (a hardcoded stand-in, so it runs with no API key) for
> `--oracle claude` and the ordering is decided for real, one question at a
> time. `--oracle manual` prints each question and lets you answer it yourself.

ChatCPU contains a custom CPU, RAM, ROM, assembler, shell, persistent filesystem and memory mapped I/O.

It does **not** require external packages or a server. The project is specifically designed around the sandboxed Python environment available in ChatGPT.

The persistent filesystem uses the browser's **Cache Storage API**, meaning files can survive between separate Python runs even though the Python runtime itself is reset.

## Getting started

ChatCPU is not currently a normal Python program that you install and run locally.

To start it inside the supported environment:

1. Open ChatGPT's Python code runner
2. Run the ChatCPU bootstrap code that provides the `js` bindings
3. Run `/minios/boot.py`
4. The boot process loads the MiniOS shell from the persistent filesystem
5. Use the shell commands provided by the running environment

The important thing to understand is that **the Python runtime and the ChatCPU filesystem are separate**.

The Python variables are temporary. The Cache backed filesystem is persistent.

If the Python environment is restarted, the bootstrap/boot code has to be executed again. Files stored in the ChatCPU filesystem can remain available after that.

## Current status

**ChatCPU 2.x**

The project currently includes:

* 16 bit CPU architecture
* A, B, C and D registers
* Program Counter and Stack Pointer
* Zero, Carry and Negative flags
* 64 KiB RAM
* 64 KiB ROM
* 16 bit addressing
* Custom assembler
* Labels and multi byte instructions in the newer architecture
* Memory mapped I/O
* Text based display
* Keyboard input queue
* Random number I/O
* Persistent filesystem
* MiniOS shell
* Assembly programs
* CPU register and memory inspection

## Architecture

### CPU

ChatCPU uses a custom 16 bit CPU architecture.

The CPU contains:

```text
A   B   C   D
PC  SP
Z   CF  N
```

The CPU executes programs stored in ROM and uses RAM for runtime data.

The stack grows downward from `0xFFFF`.

### Memory

```text
RAM: 64 KiB
ROM: 64 KiB
Address size: 16 bit
```

16 bit values are stored using two bytes in little endian order.

### Display

The current text display is:

```text
32 × 16 characters
```

It is a simple text framebuffer rather than a graphical display.

### I/O

Current I/O ports include:

|   Port | Name             | Purpose                    |
| -----: | ---------------- | -------------------------- |
| `0x00` | `PORT_KEY`       | Read keyboard input        |
| `0x01` | `PORT_KEY_STATE` | Check whether input exists |
| `0x10` | `PORT_SCREEN`    | Write to the text display  |
| `0x20` | `PORT_RANDOM`    | Random number input        |

## Filesystem

ChatCPU uses the browser's Cache Storage API as its persistent disk.

The filesystem is represented using `Request` and `Response` objects stored inside a Cache.

Example layout:

```text
/minios/
├── boot.py
├── kernel.py
├── runtime.py
├── shell.py
├── config.cfg
├── system.cfg
├── version.txt
├── programs/
│   ├── hello.asm
│   ├── input.asm
│   ├── screen.asm
│   └── snake.asm
└── shell/
    ├── terminal.py
    ├── commands.txt
    └── output.txt
```

The exact files may change during development.

## Shell

MiniOS provides commands for interacting with the system.

Common commands include:

```text
help
ls
cat <file>
disk
version

regs
mem <address>
reset

asm <file>
run <file>

clear
```

For example:

```text
ls
```

lists files on the persistent ChatCPU filesystem.

```text
regs
```

shows the CPU registers and flags.

```text
mem 0x0000
```

inspects RAM.

## Assembly

ChatCPU has its own assembly language.

A simple program can look like:

```asm
LDIA 'H'
OUT

LDIA 'i'
OUT

HLT
```

The assembler converts the source into ChatCPU machine code.

Programs are stored under:

```text
/minios/programs/
```

## Instruction set

The instruction set is still evolving.

Some of the currently implemented instructions include:

| Mnemonic | Opcode | Description              |
| -------- | -----: | ------------------------ |
| `NOP`    | `0x00` | No operation             |
| `LDIA`   | `0x01` | Load 16 bit value into A |
| `LDIB`   | `0x02` | Load 16 bit value into B |
| `ADD`    | `0x05` | Add B to A               |
| `SUB`    | `0x06` | Subtract B from A        |
| `INC`    | `0x07` | Increment A              |
| `DEC`    | `0x08` | Decrement A              |
| `OUT`    | `0x13` | Output A as a character  |
| `HLT`    | `0x15` | Halt CPU                 |
| `INP`    | `0x20` | Read an I/O port         |
| `OUTP`   | `0x21` | Write to an I/O port     |

The instruction set and implementation are actively changing, so this table may not always perfectly match the latest development version.

## Example

A basic Hello World program:

```asm
LDIA 'H'
OUT

LDIA 'e'
OUT

LDIA 'l'
OUT

LDIA 'l'
OUT

LDIA 'o'
OUT

LDIA ' '
OUT

LDIA 'C'
OUT

LDIA 'h'
OUT

LDIA 'a'
OUT

LDIA 't'
OUT

LDIA 'C'
OUT

LDIA 'P'
OUT

LDIA 'U'
OUT

HLT
```

## Snake

One of the current test programs is a Snake implementation.

It uses ChatCPU's:

* keyboard input
* screen output
* CPU execution
* RAM
* persistent storage

The game has already been tested running inside the ChatGPT Python environment.

Current saved game state can be stored separately from the Python runtime.

## Known limitations

* ChatCPU depends on the Python environment provided by ChatGPT
* It is not currently a normal standalone Python application
* The `js` bindings used by the persistent filesystem are environment specific
* The Python namespace is reset between runs
* The Cache Storage filesystem can persist between runs
* The physical browser keyboard is not automatically connected to the CPU
* Graphics are currently limited to a text based display
* There are no proper hardware interrupts
* CPU execution has a cycle limit to prevent infinite loops from locking the runner
* The instruction set is still under development
* Some files in the development filesystem may be incomplete or experimental

## Can it run DOOM?

No.

Not *yet* anyway

A real DOOM port would require substantially more complete CPU functionality, graphics, input, memory management and a much more capable runtime environment.

Getting persistent storage working in the sandbox was already kind of insane.

## Why does this exist?

Because apparently building a fake computer inside ChatGPT's Python runner sounded like a good idea.

Also funnies :)
