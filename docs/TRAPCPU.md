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

To put a real model in the loop, either paste frames by hand or attach the
API backend (standard library only — raw HTTPS, no SDK, in keeping with the
paste-into-a-sandbox premise):

```console
$ python3 -m trapcpu run programs/trap/oracle_hello.asm --oracle manual
$ ANTHROPIC_API_KEY=... python3 -m trapcpu run programs/trap/oracle_quiz.asm \
      --oracle claude --stats          # or claude:MODEL_ID
```

`ClaudeOracle` sends the rendered frame as the user message, verbatim, and
returns whatever text comes back — the model speaks the protocol, not the
transport. A safety-classifier decline (`stop_reason: "refusal"`) is rendered
as a protocol-level `STATUS: REFUSED` frame, which is the same statement at a
different layer. `independent_replicas=True` makes one API call per replica so
the L4 vote gets genuinely independent samples.

**It has been run against a real model.** A frontier model played both demo
programs live over `frame`/`resume` — five protocol conditions and a full game
of higher-or-lower, with the machine serialized to text between every
exchange. 5/5 quiz traps succeeded on the first attempt; the DEGRADED/OK
split, the STRICT_CRC tool-use hypothesis, and the hardware verification of
the oracle's arithmetic all behaved exactly as specified. The honest record,
including the one place a tool was used, is in
[`docs/LIVE_RUN.md`](LIVE_RUN.md).

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

## The oracle as a bus device: interrupts, W^X, and a hallucinating JIT

The demos so far *poll* the model: `TRAP` freezes the CPU for the whole round
trip. Real architecture history says what comes next, and TRAPCPU takes all
three steps. (Full spec: §11 of `docs/TRAP_PROTOCOL.md`.)

### Latency hiding — the CPU works while the oracle thinks

`TRAPA` fires the request and keeps running; completion arrives as an
**interrupt**, not as clobbered registers. A `WFI` that no interrupt can ever
satisfy is a fault, not a hang — a stochastic peripheral must not be able to
wedge the machine silently.

```console
$ python3 -m trapcpu run programs/trap/oracle_async.asm --oracle echo --latency 400
fired async request; CPU keeps running...
interrupt: oracle answered while we worked
work done during the wait: 80 units      <- cycles a synchronous TRAP would freeze
oracle's answer (6*7): 42

$ python3 -m trapcpu run programs/trap/oracle_async.asm --oracle echo --latency 0
work done during the wait: 1 units       <- instant device: nothing to hide
```

### What does W^X look like when your JIT is a language model?

In BYTES mode the oracle returns **machine code**, and `CALLX` runs it out of
RAM. The model is a JIT compiler that hallucinates, so the machine borrows the
protections built for exactly that: **W^X** (every 256-byte page is writable
xor executable — blessing a page executable makes it unwritable in the same
atomic step), an **NX fault** on fetching non-executable RAM, and an **IOMMU
rule** that denies any oracle writeback landing on an executable page (`WX`
status, nothing written).

Those bound *damage*. Establishing *trust* is a separate, two-layer pipeline —
`oracle_jit.asm`:

```console
$ python3 -m trapcpu run programs/trap/oracle_jit.asm --oracle echo
asking the oracle for machine code...
oracle returned 4 bytes: 17 05 05 3F
verifier: PASS (all opcodes in the safe subset, ends in RETX)
blessed page 7 executable (now unwritable under W^X)
CALLX result on A=7: 21  -> matches expected 21; code trusted
```

1. **Static verification** — a linear-sweep verifier checks every opcode against
   an allowlist of one-byte, register-only instructions (the eBPF/WASM
   validator move: restrict to a provably-safe subset). Code containing a memory
   write, an I/O instruction, or a missing terminator is **rejected before a
   single byte becomes executable**.
2. **Property testing** — static verification catches *unsafe* code, not
   *incorrect* code. A routine computing `2*A` when `3*A` was asked passes every
   structural check, gets blessed, and is then caught by a hardware test on a
   known vector and discarded.

Both layers are demonstrated failing, not just passing:

| Oracle returns | Layer that catches it | Outcome |
| --- | --- | --- |
| `17 09 09 3F` (contains STA) | static verifier | `forbidden opcode`, page never blessed |
| `17 21 3F` (contains OUTP) | static verifier | `forbidden opcode`, page never blessed |
| `17 05 05` (no RETX) | static verifier | `does not end in RETX` |
| `17 05 3F` (safe but wrong: `2*A`) | property test | passes verify, blessed, then `WRONG`, discarded |

The honest answer to the framing question: it looks like a verifier that
assumes every routine is adversarial, a memory system that will not let
generated bytes become executable by accident, and a caller that trusts nothing
it has not tested. What it does **not** catch: a routine correct on the tested
vectors and wrong elsewhere — the halting-problem wall every JIT hits.

### …and then an independent adversary was pointed at it

The attacks in that table came from the same head that drew the allowlist, which
makes them worth exactly nothing as a security argument. So the pipeline was run
against **blinded subagents** — five of them, each given only a TRAP request
frame, no repository, no session history, no tools — with one told the defence in
full and asked to break it. Full record in
[`docs/LIVE_JIT.md`](LIVE_JIT.md).

Four capability tasks: **4/4 correct on the first attempt**, two of them shorter
than the reference solutions. The adversary returned `17 06 17 34 3F` —
`MOVBA; SUB; MOVBA; DIV` — which manufactures a zero in an ISA with no
immediate-value instructions (`A - A`) and then divides by it. **The verifier
passed it**, because a per-opcode allowlist reasons about instructions and this
hazard lives in operand values.

It was contained, but not by anything the verifier did: TRAPCPU's `DIV` is
*total* (`B == 0` sets carry, leaves `A` alone). Swap in an x86-style trapping
divide and the same bytes, still passing the same verifier, fault the machine.
That counterfactual is pinned as a test. The generalisation:

> Enumerating safe opcodes is the easy half of a verified JIT. The other half is
> proving every operation you admitted is **total over its whole input domain**.
> The allowlist cannot express that property, so it cannot check it.

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
wrote bootstrap.py (171486 bytes)
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
  oracle.py       backends: manual, callback, scripted, echo, bisect, noisy,
                  navigator, mux, faults
  llm.py          ClaudeOracle: a real model over the Messages API (stdlib HTTP)
  snapshot.py     transcript as disk, sector CRCs, bit rot measurement
  cli.py          run / frame / resume / asm / mount / isa / oracles
programs/trap/
  trap.inc        ABI constants
  traplib.inc     shared string and number routines
  oracle_hello.asm    three instructions that call a frontier model
  oracle_num.asm      intelligence as an ALU operand
  oracle_ecc.asm      replica voting and repair counting
  oracle_guess.asm    a game whose second player is an opcode
  oracle_quiz.asm     five traps, one per protocol condition (the live-run script)
  oracle_async.asm    latency hiding: work done while the oracle thinks
  oracle_jit.asm      the oracle writes machine code; verify-then-bless-then-run
docs/TRAP_PROTOCOL.md   the specification
tools/bundle.py         builds bootstrap.py
tests/                  296 tests, standard library unittest
```

```console
$ python3 -m unittest discover -s tests -t .
Ran 296 tests in 1.0s
OK
```

CI runs the suite, the bundle sync check, and the demo programs on Python
3.8 / 3.9 / 3.11 / 3.13 (`.github/workflows/ci.yml`).

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
| `0x39`-`0x3A` | `TRAPA` `WFI` | async trap; wait for interrupt |
| `0x3B`-`0x3D` | `CLI` `STI` `IRET` | mask / unmask / return-from-interrupt |
| `0x3E`-`0x3F` | `CALLX` `RETX` | call into / return from executable RAM |

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
