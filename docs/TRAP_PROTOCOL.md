# The TRAP Protocol, version 1

> How a 16-bit machine calls a language model, and what it does when the answer
> is wrong.

TRAPCPU treats a language model as a memory-mapped coprocessor at I/O port
`0x30`. A program builds a request descriptor in RAM, writes its address to the
port, and executes `TRAP`. The machine suspends. A request frame is published
into the conversation. Whatever text comes back is parsed, validated, and
written into RAM before the instruction after `TRAP` retires.

This document is the contract between the three parties:

| Party | Role |
| --- | --- |
| **guest program** | builds descriptors, reads status codes, handles failure |
| **emulator** | renders frames, validates replies, writes memory, counts everything |
| **oracle** | a model, a human, or a script, that answers frames |

The emulator trusts neither of the other two. The guest may point the
descriptor anywhere; the oracle may say anything at all.

---

## 1. Why this is a protocol and not a function call

A function call has three properties this does not have: it terminates, it
returns the type it promised, and calling it twice with the same argument does
the same thing. Port `0x30` gives up all three. What replaces them is a
protocol with explicit failure modes, and the whole design follows from three
observations.

**The transport is lossless; the component is not.** There is no line noise
between the emulator and the model. Nothing flips a bit in flight. What
corrupts a payload is the model deciding to be helpful, so error *detection*
has to target protocol violations rather than transmission errors, and error
*correction* cannot be a code over the bits — it has to be redundancy over
independent samples. See §7.

**A checksum a model cannot compute is not a checksum.** CRC-16 over the
payload is trivially checkable by the emulator and effectively uncomputable by
a model without tools. So `LEN` is mandatory (a model can count) and `CRC`/`SUM`
are verified only when offered. A reply with no checksum completes as
`DEGRADED`, not `OK`: usable, and counted. See §5.3.

**The retry is where staleness lives.** A conversation is an append-only log
that both parties can re-read. If a retry reused its nonce, an answer to the
previous attempt — still sitting in the transcript — would satisfy it. Every
attempt therefore gets a fresh nonce, and a reply carrying any other nonce is
rejected as `BAD_NONCE` no matter how good its content is. See §6.

---

## 2. The request descriptor

24 bytes in RAM. Fields marked **out** are written by the emulator when the
trap completes.

| Offset | Size | Field | Notes |
| ---: | ---: | --- | --- |
| `0x00` | 2 | `MAGIC` | `0x524F`, ASCII `OR` |
| `0x02` | 1 | `VERSION` | protocol version, `1` |
| `0x03` | 1 | `MODE` | `0` TEXT, `1` NUM, `2` BYTES |
| `0x04` | 2 | `PROMPT` | pointer to a NUL-terminated prompt |
| `0x06` | 2 | `RESPONSE` | pointer to the response buffer |
| `0x08` | 2 | `CAPACITY` | response buffer size in bytes, non-zero |
| `0x0A` | 1 | `REPLICAS` | independent samples, 1..9 (`0` reads as 1) |
| `0x0B` | 1 | `RETRIES` | protocol retries before giving up, 0..15 |
| `0x0C` | 2 | `STATUS` | **out** completion code (§4) |
| `0x0E` | 2 | `LENGTH` | **out** bytes written to the buffer |
| `0x10` | 2 | `NONCE` | **out** nonce of the final attempt |
| `0x12` | 1 | `ATTEMPTS` | **out** attempts consumed |
| `0x13` | 1 | `CORRECTED` | **out** byte positions repaired by the vote |
| `0x14` | 2 | `FLAGS` | behaviour bits (§2.1) |
| `0x16` | 2 | `VALUE` | **out** decoded integer, NUM mode |

All 16-bit fields are little endian, matching the rest of the machine.

### 2.1 Flags

| Bit | Name | Effect |
| ---: | --- | --- |
| `0x0001` | `STRICT_CRC` | a reply without `CRC`/`SUM` fails instead of degrading |
| `0x0002` | `NO_TRIM` | keep leading and trailing whitespace in the payload |
| `0x0004` | `ALLOW_TRUNCATE` | clip an overlong payload instead of failing |
| `0x0008` | `NO_NUL` | TEXT mode: do not append a terminator |

### 2.2 Descriptor validation

Before anything is published, the emulator checks: the pointer is in bounds;
`MAGIC` and `VERSION` match; `MODE` is known; `REPLICAS` ≤ 9; `RETRIES` ≤ 15;
`CAPACITY` is non-zero; `RESPONSE + CAPACITY` is in bounds; the prompt is
non-empty and NUL-terminated within 4096 bytes.

A rejected descriptor completes the trap **without suspending**, with
`BAD_DESCRIPTOR` (or `NO_REQUEST` if nothing was ever latched onto the port).

One subtlety worth stating, because it is the sort of thing that becomes a
security bug: the status writeback happens only once `MAGIC` and `VERSION` have
verified. A pointer that fails those has not proven it points at a descriptor,
and writing 12 bytes of status through it would be a wild store into guest
memory. Such a trap reports itself in `A` alone.

---

## 3. Frames

### 3.1 Anchoring

Every frame marker must appear **at column zero on a line of its own**. This is
load-bearing. The request frame contains a copy of the reply template so the
oracle has something to imitate, and that copy is indented by two spaces. A
request therefore cannot be parsed as a reply — which matters enormously once
transcripts are being re-read as storage (§8), because otherwise a machine
could satisfy its own trap with the template it just printed.

The same rule protects the emulator from the *guest*: prompt text is copied out
of guest RAM, so a program could otherwise write `=== TRAPCPU END ===` into its
own prompt and truncate the frame containing it. Any prompt line that would
forge a marker is emitted with one leading space.

### 3.2 Request frame

```
=== TRAPCPU/1 ORACLE REQUEST ===
NONCE: A3F1
ATTEMPT: 1/4
MODE: NUM
REPLICAS: 1
MAXLEN: 8
CYCLE: 2535
DESC: 0300
REGS: A=0300 B=03C4 C=0000 D=0014 PC=0035 SP=FFFF Z=0 CF=0 N=0
RETRY_REASON: BAD_NONCE (nonce 3CEB != 3CEC)      <- only on attempts after the first
PROMPT>>>
We are playing higher or lower. I am thinking of a whole number from 1 to 100.
<<<PROMPT

REPLY WITH ONE FRAME IN EXACTLY THIS FORM, AT COLUMN ZERO,
WITH NO SURROUNDING PROSE AND NO CODE FENCES:

  === TRAPCPU/1 ORACLE REPLY ===
  NONCE: A3F1
  STATUS: OK
  LEN: <payload length in bytes>
  PAYLOAD>>>
  <answer>
  <<<PAYLOAD
  === TRAPCPU END ===

RULES:
- NONCE must be exactly A3F1. Any other value is rejected as a stale reply.
- LEN is the number of UTF-8 bytes in the payload after leading and trailing
  whitespace is stripped.
- The payload must be at most 8 bytes.
- MODE is NUM: the payload must be a single integer in 0..65535, digits only.
- CRC/SUM lines are optional. Include one only if you can compute it exactly.
- If you will not answer, reply with the same frame and STATUS: REFUSED and no
  payload blocks.
=== TRAPCPU END ===
```

`REGS` and `CYCLE` are informational. They exist because a frame published into
a conversation is the only artifact of a suspended machine, and being able to
read the machine's state off the wire turns a bug report into a diagnosis.

### 3.3 Reply frame

```
=== TRAPCPU/1 ORACLE REPLY ===
NONCE: A3F1
STATUS: OK
LEN: 4
CRC: 86D3
PAYLOAD>>>
BLUE
<<<PAYLOAD
=== TRAPCPU END ===
```

Grammar:

```
reply       := BEGIN header* block+ END
BEGIN       := "=== TRAPCPU/" version " ORACLE REPLY ==="
END         := "=== TRAPCPU END ==="
header      := KEY ": " value          ; NONCE and STATUS are frame level
block       := block-header* "PAYLOAD>>>" line* "<<<PAYLOAD"
block-header:= "REPLICA: " n | "LEN: " n | "CRC: " hex4 | "SUM: " n
```

* `NONCE` — required, must equal the request's.
* `STATUS` — `OK` or `REFUSED`. Absent reads as `OK`. Anything else is `MALFORMED`.
* `LEN` — required per block.
* `CRC` — optional, CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`), four
  uppercase hex digits.
* `SUM` — optional, sum of payload bytes mod 65536, decimal.
* `REPLICA` — cosmetic; blocks are matched by order, not by this number.

The number of payload blocks must equal `REPLICAS` exactly.

**Payload normalisation.** The payload is the lines between the fences joined
with `\n`, then stripped of leading and trailing whitespace unless `NO_TRIM` is
set. `LEN` is checked against the UTF-8 byte length of the result. A payload
cannot contain a line that is exactly `<<<PAYLOAD`.

**Tolerated deviations.** Prose before and after the frame, markdown code
fences around it, and indented header lines inside the frame body are all
accepted. These are the things models do constantly, and none of them can
change the payload. Everything that *can* change the payload is rejected.

**Frame selection.** If the text holds several reply frames, the last one whose
nonce matches wins. If none match, the last frame overall is used, so the error
reported is `BAD_NONCE` rather than a vague parse failure.

---

## 4. Status codes

`A` receives the status; the descriptor receives it too. `CF` is clear on
success and set on failure, so `JNC`/`JC` branch on it directly.

| Code | Name | Meaning |
| ---: | --- | --- |
| `0x00` | `OK` | accepted, integrity fully verified |
| `0x01` | `DEGRADED` | accepted, but no checksum was supplied |
| `0x10` | `NO_REQUEST` | `TRAP` with no descriptor latched |
| `0x11` | `BAD_DESCRIPTOR` | descriptor failed validation (§2.2) |
| `0x12` | `NO_FRAME` | reply contained no reply frame |
| `0x13` | `BAD_VERSION` | frame version this machine does not speak |
| `0x14` | `BAD_NONCE` | stale or mismatched nonce |
| `0x15` | `MALFORMED` | broken fences, wrong block count, unknown status |
| `0x16` | `BAD_LENGTH` | declared `LEN` disagrees with the payload |
| `0x17` | `BAD_CHECKSUM` | `CRC`/`SUM` mismatch, or absent under `STRICT_CRC` |
| `0x18` | `OVERFLOW` | payload longer than `CAPACITY` |
| `0x19` | `BAD_ENCODING` | mode-specific decode failure |
| `0x1A` | `NO_QUORUM` | replicas disagreed with no majority |
| `0x20` | `REFUSED` | the oracle declined |
| `0x21` | `RETRIES` | the host produced no reply at all |
| `0x22` | `BUDGET` | the oracle call budget is spent |
| `0x23` | `ABORT` | the host cancelled the trap |

Success is `A < 2`. In assembly:

```asm
        LDIB 2
        CMP                     ; CF set when A - 2 borrows, i.e. A < 2
        JC   SUCCESS
```

`OK` and `DEGRADED` are distinct on purpose. A program that must not act on
unverified data can test for `OK` exactly; everything else can treat both as
success and let the statistics record how often verification was skipped.

---

## 5. The validation ladder

Each layer has its own status code, so a failure names itself instead of
collapsing into "the model was wrong". Layers run in order and stop at the
first failure.

| Layer | Checks | Failure |
| --- | --- | --- |
| **L0** framing | a terminated frame exists, version is known | `NO_FRAME`, `MALFORMED`, `BAD_VERSION` |
| **L1** addressing | nonce matches; status word is `OK`/`REFUSED`; block count equals `REPLICAS` | `BAD_NONCE`, `REFUSED`, `MALFORMED` |
| **L2** length | every block's `LEN` matches its payload | `BAD_LENGTH` |
| **L3** checksum | `CRC`/`SUM` verify when present; `STRICT_CRC` honoured | `BAD_CHECKSUM` |
| **L4** redundancy | replicas elect a payload by strict majority | `NO_QUORUM` |
| **L5** encoding | NUM parses as `0..65535`; BYTES is hex digit pairs | `BAD_ENCODING` |
| **L6** capacity | the decoded payload fits, or `ALLOW_TRUNCATE` is set | `OVERFLOW` |

### 5.1 Why L2 catches most real corruption

`LEN` is checked before the checksum and before the vote, which means the
cheapest field catches the commonest failure. A model that appends "Let me know
if you need anything else!" inside the fences, or truncates, or answers in a
sentence when a word was requested, produces a length mismatch every time. In
the fault-injection suite (§9) three distinct corruptions land on `BAD_LENGTH`
before any checksum is consulted.

The gap this leaves is worth naming: a *same-length* substitution — `BLUE`
becoming `BLXE` — passes L2 untouched. Only a checksum (L3) or a vote (L4)
catches that one, which is the clearest argument for asking for either.

### 5.2 What L3 is actually for

The transport cannot corrupt anything, so a mismatching CRC does not mean the
bits got damaged in flight. It means the oracle wrote a checksum that does not
describe what it sent — either it miscomputed, or it edited the payload after
computing. Both are protocol violations worth failing on, and both are things a
tool-using oracle should never do. Treat `BAD_CHECKSUM` as a compliance signal,
not an integrity one.

### 5.3 The `DEGRADED` bargain

Requiring `STRICT_CRC` of a plain chat model means requiring arithmetic it
cannot reliably do, and the result is a machine that fails constantly for
reasons unrelated to the answer's quality. Requiring nothing means never knowing
whether anything was verified. `DEGRADED` splits the difference and makes the
gap a number: `stats.degraded / stats.traps` is the fraction of this program's
run that proceeded on unverified data.

---

## 6. Retries

```
attempt 1 ── validate ── OK ──────────────────► write RAM, resume
                │
                └── fail ── retries left? ── yes ──► new nonce, republish
                                    │
                                    no ──► write the specific status, resume
```

* Each attempt draws a **fresh nonce**. A reply to attempt *n* cannot satisfy
  attempt *n+1*, which is what keeps a re-read transcript from answering the
  wrong question.
* The final status is the **specific last failure**, not a generic "retries
  exhausted". A program that gave up after three `BAD_ENCODING`s learns that
  the model cannot follow the format, which is different from three `REFUSED`s.
* Retries consume `RETRIES + 1` attempts at most and **one** unit of the oracle
  budget. The budget bounds calls; `RETRIES` bounds effort inside a call.
* The retry frame carries `RETRY_REASON`, so the oracle is told what was wrong
  with its last answer. This measurably raises the odds the second attempt
  succeeds, and costs nothing.

---

## 7. Redundancy: ECC for a memory that hallucinates

Set `REPLICAS` to *n* > 1 and the oracle is asked to answer *n* times
independently. The emulator then elects a payload in two rounds, **both
requiring a strict majority, never a plurality**:

1. **Length.** A replica that disagrees about how long the answer is has
   answered a different question. Folding it into the byte vote would smear the
   result, so minority lengths are discarded first and counted in `discarded`.
2. **Each byte position** among the survivors.

A position with no majority is an uncorrectable error and fails the whole
election with `NO_QUORUM`, exactly as an ECC word with too many flipped bits
does. `CORRECTED` counts the positions where the elected byte differed from at
least one surviving replica: those are errors this machine repaired in
software, on a memory it does not trust.

The parallel to real ECC is exact, including the unhappy parts:

| Replicas | Detects | Corrects | Analogue |
| ---: | --- | --- | --- |
| 1 | nothing | nothing | plain DRAM |
| 2 | any single disagreement | nothing | mirrored pair |
| 3 | up to two | one | SECDED, roughly |
| 5 | up to four | two | wider code |

Two consequences worth stating, because both surprised the test suite:

**Even light noise can fail.** With five replicas and roughly one corrupted byte
each, two replicas occasionally corrupt the *same* position differently, leaving
2/5 for the leader and no majority. That is `NO_QUORUM`, and it is the design
working: the alternative is electing a plurality, which would mean writing a
byte that most replicas disagreed with. Failing loudly is strictly better than a
1-in-5 chance of being quietly wrong. The property that actually holds is the
one worth relying on: **a successful vote is a correct vote**, and the tests
assert exactly that rather than a success rate.

**The parallel to ECC breaks in one important place.** Real ECC assumes bit
errors are independent. Replicas from one model are correlated — a model that is
confidently wrong is confidently wrong five times, the vote is unanimous, and
the answer is garbage. **Redundancy protects against variance, not against
bias.** `CORRECTED` counts the errors it caught; it says nothing about the
errors that agreed with each other. Do not read a clean vote as a correct
answer.

---

## 8. The transcript as disk

ChatCPU persisted through the browser's Cache Storage. TRAPCPU persists through
the conversation: a snapshot is a text frame, emitted into the chat and scanned
back out on the next boot. The storage medium is the attention window.

```
=== TRAPCPU/1 SNAPSHOT ===
GEN: 3
NAME: colour
CYCLE: 4211
STATE: TRAPPED
REGS: A=0000 B=0000 C=0000 D=0000 PC=0031 SP=FFFF Z=1 CF=0 N=0
ORACLE: traps=3 attempts=4 ok=2 degraded=1 failed=0 retried=1 corrected=0 ...
LASTSTATUS: OK
PENDING: nonce=A3F1 attempt=2 retries=3 mode=0 replicas=1 capacity=32 ...
PENDINGPROMPT>>>
Name a colour.
<<<PENDINGPROMPT
ROMMAP: 0000,0020
RAMMAP: 0300,0320,0400
ROM>>>
@0000 0103000021301522...  1F2A
<<<ROM
RAM>>>
@0300 4F52010004050020...  9B14
<<<RAM
SNAPCRC: 77C3
=== TRAPCPU END ===
```

Three properties make this a disk rather than a hope:

**Sectors.** Memory is written as 32-byte aligned chunks, each with its own
CRC. Runs of zeros are skipped, so a 64 KiB machine snapshots in a few hundred
bytes. A chunk whose CRC fails is reported as a bad sector and left zeroed —
never restored as plausible garbage.

**An allocation map.** `ROMMAP` and `RAMMAP` list the addresses of every sector
written. Without them, a sector deleted from the transcript is simply absent and
nothing notices. With them, `mount` names the addresses that went missing. This
distinction is tested directly: strip the map, evict some sectors, and the loss
becomes silent.

**Generations.** `GEN` increments per snapshot, and `mount` picks the highest
generation it can still fully parse. A damaged newer snapshot does not destroy
an older intact one; both remain in the transcript.

A snapshot taken while `STATE: TRAPPED` carries the pending request, including
the prompt, so a machine can be suspended mid-trap, lose its entire runtime, be
reconstructed from the conversation, and accept the reply it was waiting for.
That round trip is the load-bearing claim of this layer and it has its own tests.

### 8.1 Bit rot, measured

Context eviction is bit rot with a different name, and it has two distinct
signatures:

| Damage | Detected by | Reported as |
| --- | --- | --- |
| characters mangled | sector CRC | `sector CRC mismatch` |
| whole lines removed | allocation map | `sector missing from transcript (evicted)` |
| header lines altered | `SNAPCRC` | degraded mount, sectors still checked individually |
| `REGS` line lost | mount refuses | `SnapshotError` |

`MountReport.bytes_lost` is the figure. It is a real number about a real
failure mode: how much of this computer's memory did the conversation forget.

Losing the `REGS` line is deliberately fatal. Registers cannot be inferred, and
a machine restored with guessed registers would run and produce wrong answers,
which is worse than not booting.

---

## 9. Fault taxonomy

Each of these is a behaviour observed from real models, and each is injectable
(`--fault NAME`) so the scaffolding can be tested without a stochastic component
attached.

| Fault | What the oracle does | Caught at | Status |
| --- | --- | --- | --- |
| `drop` | says nothing | host | `RETRIES` |
| `silence` | prose, no frame | L0 | `NO_FRAME` |
| `unterminated` | forgets the END marker | L0 | `MALFORMED` |
| `stale_nonce` | answers the previous question | L1 | `BAD_NONCE` |
| `bad_nonce` | mangles the nonce | L1 | `BAD_NONCE` |
| `extra_block` | more blocks than replicas | L1 | `MALFORMED` |
| `refuse` | declines | L1 | `REFUSED` |
| `bad_length` | miscounts bytes | L2 | `BAD_LENGTH` |
| `truncate` | drops the tail | L2 | `BAD_LENGTH` |
| `wrong_mode` | prose where a number was demanded | L2/L5 | `BAD_LENGTH`/`BAD_ENCODING` |
| `bitflip` | one character changes | L3 | `BAD_CHECKSUM` |
| `bad_checksum` | checksum does not match | L3 | `BAD_CHECKSUM` |
| `no_checksum` | omits CRC and SUM | L3 | `DEGRADED` |
| `chatty` | wraps the frame in explanation | — | `OK` |
| `fenced` | wraps the frame in a code fence | — | `OK` |

The last two are in the table because passing is the correct outcome. A
protocol that fails on politeness will fail constantly in production for
reasons that have nothing to do with correctness.

### 9.1 What this protocol does not defend against

Stating the gaps plainly, because a threat model with no gaps is a threat model
nobody checked:

* **A confidently wrong answer that satisfies every layer.** Well-formed,
  correctly counted, correctly checksummed, unanimous across replicas, and
  false. Nothing here detects that. Only the guest program can, by asking a
  question whose answer it can verify.
* **Correlated replica error.** §7.
* **A prompt whose content manipulates the oracle.** The framing is protected
  (§3.1); the semantics are not. A guest program that builds prompts from
  untrusted data can talk the oracle into anything the oracle would do.
* **Timing.** There is no wall-clock timeout, because there is no wall clock in
  a suspended machine. `RETRIES` bounds attempts and the oracle budget bounds
  calls; neither bounds how long a human takes to paste a reply.

---

## 10. Conformance checklist

An implementation speaks TRAP/1 if:

1. Frame markers are recognised only at column zero, and emitted request
   templates are indented.
2. Every attempt uses a nonce distinct from the attempt before it.
3. A reply whose nonce does not match is rejected regardless of content.
4. `LEN` is required and checked after normalisation.
5. `CRC`/`SUM` are verified when present; their absence yields `DEGRADED` unless
   `STRICT_CRC`, in which case it yields `BAD_CHECKSUM`.
6. Replica election requires a strict majority on length and on every byte;
   anything less is `NO_QUORUM`.
7. A failed trap writes its specific status, never a generic one.
8. Nothing is written to the response buffer unless the trap succeeded.
9. A descriptor that fails `MAGIC`/`VERSION` gets no status writeback.
10. Snapshot sectors carry individual CRCs and an allocation map, and a mount
    reports every sector it could not restore.

Points 8 and 9 are the ones that are easy to get wrong and hard to notice: both
are cases where the tidy-looking behaviour (always write something back) is the
one that silently corrupts guest memory.

---

## 11. Extensions: the oracle as a bus device (protocol v1.1)

Everything above treats the oracle as a *polled* device: `TRAP` stops the CPU
dead and waits. That is honest but pessimistic — a peripheral that takes twenty
seconds to answer should not freeze a machine that has other work to do, and a
device that can only be *read* is weaker than one that can be *trusted to
write*. Three extensions, each mapping a real hardware idea onto the stochastic
peripheral. They are additive: a v1 oracle and a v1 program still work
unchanged.

### 11.1 The asynchronous channel — interrupts

`TRAPA` publishes the same request frame as `TRAP` but does **not** suspend.
The frame carries a `CHANNEL: ASYNC` header; the machine keeps executing, and
completion is delivered as an **interrupt** rather than as clobbered registers.

```
TRAPA ──▶ publish frame (CHANNEL: ASYNC), keep running
             │
   ...CPU does other work while the oracle thinks...
             │
   reply validated ──▶ descriptor written back ──▶ IRQ line raised
             │
   next instruction boundary with interrupts enabled:
   push CS, push PC, mask, jump to IVEC ──▶ handler ──▶ IRET
```

* The completion writes **only the descriptor**, never the live register file.
  A handler that wants the answer reads it from the descriptor — that is what
  the descriptor's OUT fields are for. This is the rule that makes async safe:
  an interrupt that arrived between two instructions must not change what those
  instructions compute.
* `WFI` parks the machine if there is nothing else to do, and — critically — a
  `WFI` that no pending, unmasked interrupt can ever satisfy is a **fault**, not
  a hang. A stochastic peripheral that never answers must not be able to wedge
  the CPU silently.
* The channel is **single-depth**: a `TRAP` or `TRAPA` issued while a request is
  in flight completes immediately with `BUSY` (0x24). One slow device, one
  outstanding request; the guest serialises or waits.
* Ports `0x30`–`0x37` are all latch ports; the low three bits are a **device
  number**, carried as a `DEVICE: n` frame header. A fast cheap model on one
  port and a strong slow one on another is big.LITTLE for intelligence — the
  guest picks the coprocessor per question.

The measurable claim: with a device latency of *L* cycles, a synchronous `TRAP`
spends all *L* frozen; `TRAPA` spends them executing. `oracle_async.asm` prints
the hidden work directly.

### 11.2 Executable RAM and W^X — the oracle as a JIT

The boldest extension. In BYTES mode the oracle can return **machine code**, and
`CALLX` will execute it out of RAM. A language model becomes a JIT compiler —
one that hallucinates — so the architecture borrows the two protections built
for exactly that risk.

**W^X (write-xor-execute).** Every 256-byte RAM page is either writable or
executable, never both, tracked in an execute map and toggled by `PORT_MPROT`.
The instant a page is blessed executable it becomes unwritable, atomically, in
the same operation. Fetching from a non-executable page is an **NX fault**;
writing to an executable page is a **W^X fault**. Neither is recoverable — they
are guest bugs, and they say so.

**The IOMMU rule.** The oracle's writeback is DMA, and a hallucinating DMA
device must never be allowed to land bytes on a page the CPU will execute. A
completion whose response buffer overlaps *any* executable page is denied with
`WX` (0x25) and **nothing is written** — checked at completion time, because the
guest may bless a page between issuing the trap and the reply arriving. It is
the write that must be legal, not the request.

**Verify-then-bless.** These protections bound *damage*; they do not establish
*trust*. The honest pipeline, demonstrated in `oracle_jit.asm`, is two
independent layers:

1. **Static verification.** The returned bytes land in a writable scratch page.
   A linear-sweep verifier checks every opcode against an **allowlist** of
   one-byte, register-only instructions — arithmetic, logic, register moves, and
   the `RETX` terminator. No memory writes, no I/O, no jumps, no traps. A
   routine built only from these is a pure function of the register file: it can
   compute from A/B/C/D and return, and it *cannot* escape, persist, or loop.
   Anything else is rejected before a single byte becomes executable. This is
   exactly what eBPF and WASM validators do — restrict the code to a subset you
   can prove safe by inspection, rather than trying to prove arbitrary code
   safe.
2. **Property testing.** Static verification catches *unsafe* code; it says
   nothing about *correct* code. A routine that computes `2*A` when `3*A` was
   asked passes every structural check. So the guest, after blessing, runs the
   routine on a known test vector inside its sandbox and checks the answer in
   hardware before trusting it. The two layers are orthogonal and both
   necessary: the first stops the code from hurting you, the second stops you
   from believing it.

The framing question this answers: *what does W^X look like when your JIT is a
language model?* It looks like a verifier that assumes every routine is
adversarial, a memory system that will not let generated bytes become
executable by accident, and a caller that trusts nothing it has not tested.

### 11.3 New status codes and opcodes

| Code | Name | Meaning |
| ---: | --- | --- |
| `0x24` | `BUSY` | a trap was issued while the async channel was occupied |
| `0x25` | `WX` | writeback denied: the response buffer overlaps an executable page |

| Opcode | Mnemonic | Effect |
| ---: | --- | --- |
| `0x39` | `TRAPA` | asynchronous trap; publish and keep running |
| `0x3A` | `WFI` | wait for interrupt (fault if none can arrive) |
| `0x3B` / `0x3C` | `CLI` / `STI` | mask / unmask interrupts |
| `0x3D` | `IRET` | return from interrupt (pop PC, pop CS, unmask) |
| `0x3E` / `0x3F` | `CALLX` / `RETX` | call into / return from executable RAM |

Ports: `0x38` `PORT_IVEC` (interrupt vector / async channel state), `0x39`
`PORT_MPROT` (`A = (page << 8) | exec_flag`).

### 11.4 What v1.1 still does not defend against

The v1 gaps (§9.1) all stand. Two more, specific to these extensions:

* **A correct-looking routine that is wrong on untested inputs.** The property
  test checks the vectors the guest chose; a routine that passes them and fails
  elsewhere is undetected. This is the halting-problem wall that every JIT
  hits — verification proves safety, not correctness, and correctness testing
  is only ever as good as its vectors.
* **Interrupt storms and priority.** The channel is single-depth and the IRQ is
  a single line with no priority levels. A design that multiplexed many devices
  would need arbitration this deliberately omits — the honesty here is in the
  small ISA, not in pretending to be an APIC.
