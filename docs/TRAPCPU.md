# TRAPCPU

**ChatCPU built a computer inside the model's sandbox. TRAPCPU builds a
computer where the model is on the motherboard.**

Same 16-bit machine, one new I/O port, one new instruction, and an inverted
relationship. Port `0x30` is a coprocessor. A program writes a request
descriptor's address there and executes `TRAP`; the machine halts, publishes its
state and prompt into the conversation, and the model's next reply is the
hardware response — parsed off a fixed format, validated, and written into RAM
before the next instruction retires.

```asm
        LDIA REQ                ; descriptor address
        OUTP 0x30               ; latch it into the oracle controller
        TRAP                    ; the machine stops here
        ; A = status, B = length, D = decoded value.
        ; The answer is already in RAM.
```

A 6502 called a floating point chip like this.

---

## Quick start

Nothing to install. Python 3.8+, standard library only.

```console
$ python3 -m trapcpu run programs/trap/oracle_hello.asm --oracle echo
TRAPCPU asks the oracle...
oracle says: HELLO FROM THE OTHER SIDE OF THE BUS

$ python3 -m trapcpu run programs/trap/oracle_guess.asm --oracle bisect --seed 4
oracle guesses 50 -> too high
oracle guesses 25 -> too high
oracle guesses 12 -> too low
oracle guesses 18 -> too low
oracle guesses 21 -> correct!
oracle won in 5 guess(es)
```

That second one is a two-player game where the second player is an opcode. The
CPU picks a secret, assembles a fresh prompt in guest RAM each round out of
string fragments and a rendered decimal, and executes `TRAP`. The opponent's
move arrives as a validated integer in `D`.

To put a real model in the loop, use `--oracle manual` and paste:

```console
$ python3 -m trapcpu run programs/trap/oracle_hello.asm --oracle manual
```

---

## The three things this is actually about

### 1. Intelligence as an instruction

The oracle is not a library call. It is a device with a descriptor, a status
register, a completion code, and a failure taxonomy, and the guest program has
to handle all of it in assembly:

```asm
        LDIA REQ
        OUTP 0x30
        TRAP
        LDIB 2                  ; success is A < 2 (OK or DEGRADED)
        CMP
        JC   GOOD
        ; ... the model failed. A says how.
```

Modes are `TEXT`, `NUM`, and `BYTES`. In `NUM` mode the emulator guarantees a
validated 16-bit integer in `D` by the time the instruction after `TRAP` runs,
so `MUL` does not need to know its operand came from a language model.

### 2. The conversation is the disk

ChatCPU hacked Cache Storage for persistence. TRAPCPU's persistence layer is the
chat transcript. `dump(machine)` emits a snapshot frame; `mount(text)` scans any
blob of text for the newest one it can still read.

The storage medium being an attention window gives it a failure mode no real
disk has, and the layer is built to measure it rather than pretend it away.
Memory goes out as 32-byte sectors, each with its own CRC, plus an allocation
map listing every sector written:

```console
MOUNT DEGRADED gen=1 sectors=3 restored=32B lost=64B in 2 sector(s)
    bad sector rom @0000 (32B): sector CRC mismatch
    bad sector ram @0300 (32B): sector missing from transcript (evicted)
```

Mangled characters are caught by the sector CRC. Whole lines vanishing — which
is what eviction and summarisation actually look like — are caught by the map,
and only by the map. There is a test that removes the map, evicts the same
sectors, and asserts the loss becomes silent, because that is the property the
map exists to buy.

A snapshot taken mid-trap carries the pending request and its prompt, so a
machine can suspend on a `TRAP`, lose its entire runtime, be reconstructed from
the conversation, and accept the reply it was waiting for.

### 3. Deterministic scaffolding around a stochastic component

This is the part that generalises past the joke. Every engineering problem of
LLM-in-the-loop systems shows up here in 2,000 lines of Python with a status
byte on it: retry semantics, staleness, checksums, timeouts, capacity limits,
budgets, and error correction.

**The validation ladder.** Seven layers, each with its own status code, so a
failure names itself instead of collapsing into "the model was wrong":

| Layer | Checks | Failure |
| --- | --- | --- |
| L0 framing | a terminated frame exists | `NO_FRAME`, `MALFORMED` |
| L1 addressing | the nonce matches this attempt | `BAD_NONCE`, `REFUSED` |
| L2 length | declared `LEN` matches the payload | `BAD_LENGTH` |
| L3 checksum | `CRC`/`SUM` verify when present | `BAD_CHECKSUM` |
| L4 redundancy | replicas elect a payload by majority | `NO_QUORUM` |
| L5 encoding | `NUM` parses, `BYTES` is hex | `BAD_ENCODING` |
| L6 capacity | the payload fits the buffer | `OVERFLOW` |

**A fresh nonce per attempt, not per trap.** A conversation is an append-only
log both parties can re-read. If a retry reused its nonce, the answer to the
previous attempt — still sitting in the transcript — would satisfy it.

**`DEGRADED` is a real state.** `LEN` is mandatory because a model can count.
`CRC` is optional because CRC-16 is not hand-computable, and requiring it of a
plain chat model means failing constantly for reasons unrelated to the answer.
A reply with no checksum completes as `DEGRADED` — usable, and counted, so
`stats.degraded / stats.traps` tells you exactly how much of a run proceeded on
unverified data.

**Fault injection.** Fifteen named corruptions, each one a behaviour real models
actually exhibit, all injectable from the command line:

```console
$ python3 -m trapcpu run programs/trap/oracle_hello.asm \
      --fault stale_nonce,bad_length --stats
oracle says: HELLO FROM THE OTHER SIDE OF THE BUS
...
retries consumed  : 2
attempt outcomes  :
    OK               1
    BAD_NONCE        1
    BAD_LENGTH       1
```

Two of the fifteen — `chatty` (wraps the frame in explanation) and `fenced`
(wraps it in a markdown code fence) — are expected to **pass**. A protocol that
fails on politeness will fail constantly in production for reasons that have
nothing to do with correctness.

---

## What does ECC memory look like when your RAM chip hallucinates?

Set `REPLICAS` and the oracle is asked to answer *n* times independently. The
emulator elects a payload in two rounds, both requiring a **strict majority,
never a plurality**: first on length, because a replica that disagrees about how
long the answer is has answered a different question; then on each byte among
the survivors. `CORRECTED` counts the positions repaired. A position with no
majority is uncorrectable and fails as `NO_QUORUM`, exactly as an ECC word with
too many flipped bits does.

The `noisy` backend is a simulated unreliable memory: it perturbs each replica
independently, so the vote has something to actually do. `noisy:R` sets the
expected number of corrupted bytes per replica.

```console
$ python3 -m trapcpu run programs/trap/oracle_ecc.asm --oracle noisy --seed 1
elected answer : CORAL
bytes repaired : 2
replicas       : 5
status         : 0x0001         <- DEGRADED: repaired, but unchecksummed

$ python3 -m trapcpu run programs/trap/oracle_ecc.asm --oracle noisy:3 --seed 1
uncorrectable: the replicas did not agree
status         : 0x001A         <- NO_QUORUM
```

Turn the noise up far enough and the code runs out of correction, which is the
honest half of the demo. Across a sweep of rates against `CORAL`, five replicas
repair cleanly at ~1 corrupted byte each, and fall over at ~2.

The parallel to real ECC is exact, including the unhappy parts:

| Replicas | Detects | Corrects | Analogue |
| ---: | --- | --- | --- |
| 1 | nothing | nothing | plain DRAM |
| 2 | any disagreement | nothing | mirrored pair |
| 3 | up to two | one | SECDED, roughly |
| 5 | up to four | two | wider code |

And it breaks in one place that matters. Real ECC assumes bit errors are
independent. Replicas from one model are correlated: a model that is confidently
wrong is confidently wrong five times, the vote is unanimous, and the answer is
garbage. **Redundancy protects against variance, not against bias.** A clean
vote is not a correct answer, and the spec says so at length rather than
letting the demo imply otherwise.

---

## Driving it through an actual conversation

`frame` and `resume` split the loop in half so the oracle can be a chat window:

```console
$ python3 -m trapcpu frame programs/trap/oracle_guess.asm --state disk.txt
=== TRAPCPU/1 ORACLE REQUEST ===
NONCE: 4DA5
ATTEMPT: 1/4
MODE: NUM
...
PROMPT>>>
We are playing higher or lower. I am thinking of a whole number from 1 to 100.
<<<PROMPT
```

Paste that into a chat. Save the reply. Then:

```console
$ python3 -m trapcpu resume --state disk.txt --reply reply.txt
MOUNT CLEAN gen=1 sectors=35 restored=1120B
    note: resumed mid-trap: nonce 4DA5 attempt 1

--- console ---
oracle guesses 50 -> too high
---------------

=== TRAPCPU/1 ORACLE REQUEST ===
NONCE: 64C3
...
PROMPT>>>
... Your previous guess 50 was too high. Guess again, digits only.
<<<PROMPT
```

`disk.txt` is a snapshot frame, so it can *be* the transcript: `mount` scans any
text for the newest snapshot it can still read. The state of the computer lives
in the chat log between turns.

One detail that turns out to be load-bearing: **frame markers are recognised
only at column zero**, and the reply template embedded in the request is
indented two spaces. Without that, a machine re-reading a transcript could
satisfy its own trap with the template it had just printed. The same rule stops
a guest program from writing `=== TRAPCPU END ===` into its own prompt and
truncating the frame containing it.

---

## Single file bootstrap

For pasting into a sandbox that has no package manager:

```console
$ python3 tools/bundle.py
wrote bootstrap.py (142350 bytes)
```

`bootstrap.py` is the whole system flattened into one standard-library-only
file. Paste it, then:

```python
demo()                              # run the built in program
m = Machine(); m.load(assemble(SRC)); r = m.run()
print(r.frame.render())             # publish the trap into the chat
r = m.resume(reply_text)            # feed the model's answer back
print(dump(m))                      # write the machine to the transcript
m, report = mount(text)             # read it back out
```

A test asserts the bundle is in sync with the package, imports nothing outside
the standard library, and completes a trap.

---

## Layout

```
trapcpu/
  isa.py          opcode table and port map
  assembler.py    two pass assembler: DATA section, .INCLUDE, expressions
  protocol.py     the wire format and the validation ladder
  machine.py      CPU, trap suspend/resume, retry semantics, statistics
  oracle.py       backends: manual, callback, scripted, echo, bisect, faults
  snapshot.py     transcript as disk, sector CRCs, bit rot measurement
  cli.py          run / frame / resume / asm / mount / isa / oracles
programs/trap/
  trap.inc        ABI constants
  traplib.inc     shared string and number routines
  oracle_hello.asm    three instructions that call a frontier model
  oracle_num.asm      intelligence as an ALU operand
  oracle_ecc.asm      replica voting and repair counting
  oracle_guess.asm    a game whose second player is an opcode
docs/TRAP_PROTOCOL.md   the specification
tools/bundle.py         builds bootstrap.py
tests/                  242 tests, standard library unittest
```

```console
$ python3 -m unittest discover -s tests -t .
Ran 242 tests in 0.63s
OK
```

---

## Instruction set

The base ChatCPU ISA (`0x00`-`0x21`) is unchanged, so existing ChatCPU programs
still assemble and run. Everything from `0x22` up is new, and exists to make it
practical to build a prompt in RAM and walk a reply back out.

| Opcode | Mnemonic | Effect |
| ---: | --- | --- |
| `0x22` | `TRAP` | invoke the oracle; suspends the machine |
| `0x23` | `STB` | `RAM8[B] <- A` |
| `0x24` | `LDB` | `A <- RAM8[B]`, sets `Z` (string walks) |
| `0x25` | `STW` | `RAM16[B] <- A` |
| `0x26` | `LDW` | `A <- RAM16[B]` |
| `0x27`-`0x2A` | `INCB` `DECB` `INCC` `DECC` | pointer and counter steps |
| `0x2B`-`0x2E` | `MOVAC` `MOVCA` `MOVAD` `MOVDA` | register moves |
| `0x2F`-`0x32` | `JN` `JNN` `JC` `JNC` | branch on sign and carry |
| `0x33`-`0x36` | `MUL` `DIV` `SHL` `SHR` | arithmetic |
| `0x37` | `OUTS` | print the NUL-terminated string at `RAM[B]` |
| `0x38` | `CMPC` | flags from `A - C` |

`CMP` now also sets `CF` on borrow, which was previously undefined after it.
That is what makes `LDIB 2; CMP; JC ok` a clean success test.

`python3 -m trapcpu isa` prints the full table.

## Assembler

The base assembler could only emit instructions, which is fine right up to the
moment you want to hand the oracle a prompt — prompts are data. TRAPCPU adds a
`.DATA` section: code assembles into ROM, data into RAM, and the loader blits
the data segments in before execution starts.

```asm
.INCLUDE "trap.inc"

.DATA
.ORG 0x0300
REQ:    .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_TEXT
        .DW PROMPT
        .DW ANSWER
        .DW 48
        ...
PROMPT: .ASCIIZ "Name a colour."
ANSWER: .RESB 48

.CODE
        LDIA REQ
        OUTP PORT_ORACLE
        TRAP
        HLT
```

Directives: `.CODE` `.DATA` `.ORG` `.EQU` `.DB` `.DW` `.ASCII` `.ASCIIZ`
`.RESB` `.ALIGN` `.INCLUDE`. Operands take decimal, `0x`/`0b`/`0o`/`$`
literals, `'c'` character literals, labels, and `+`/`-` chains of those
(`REQ+F_STATUS`, `END-START`). Errors carry file and line, including through
includes.

---

## Known limitations

* **Nothing here detects a confidently wrong answer.** Well-formed, correctly
  counted, correctly checksummed, unanimous across replicas, and false. Only
  the guest program can catch that, by asking a question whose answer it can
  verify.
* **No wall-clock timeout**, because there is no wall clock in a suspended
  machine. `RETRIES` bounds attempts and the oracle budget bounds calls;
  neither bounds how long a human takes to paste a reply.
* **Prompt semantics are unprotected.** The framing is (§3.1 of the spec), but
  a guest that builds prompts from untrusted data can talk the oracle into
  anything the oracle would do.
* **Snapshots grow with used memory.** A program that dirties 16 KiB writes a
  32 KiB snapshot, and that is 32 KiB of context per save.
* **`OUTS` is a convenience.** It is a device instruction, not something a real
  1980s CPU would have, and it exists because every oracle program prints
  strings.

## Can it run DOOM?

No. It can lose at higher-or-lower to a frontier model in five guesses, which is
a different kind of achievement.
