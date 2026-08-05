# Live run: a frontier model on the bus

**Date:** 2026-08-05
**Oracle:** `claude-fable-5`, answering trap frames directly — no scripted
backend, no stand-in. The frames were published by the machine, read by the
model, and answered in the wire format; the replies were fed back with
`resume`. Between every exchange the machine existed only as a snapshot frame
in text.
**Machine:** TRAPCPU at the merge commit of PR #1 plus the `ClaudeOracle`
backend.

This document is the honest record. Nothing below was retried off the books,
and the one place a tool was used is labelled.

---

## Method

The conversational loop, exactly as a human would drive it against a chat
window:

```console
$ python3 -m trapcpu frame programs/trap/oracle_quiz.asm --state quiz.disk
# -> prints a request frame; the machine parks itself in quiz.disk
$ python3 -m trapcpu resume --state quiz.disk --reply reply.txt
# -> validates the reply, resumes the CPU, prints the next frame
```

Two honesty conditions, declared up front:

* **Plain condition** — the model composes the reply and counts `LEN` itself,
  and attaches no checksum. This is what an unassisted chat model can do.
* **Tool condition** — the model computes the required CRC-16 with code.
  Used exactly once, on the trap that demands it, and labelled below.

The nonces shown are the real ones issued by the machine; each attempt's
nonce comes from the emulator's RNG and the reply had to match it.

---

## Experiment 1: the five-trap quiz

`programs/trap/oracle_quiz.asm` issues five traps, one per protocol
condition. Full console transcript, assembled from the per-turn output:

```
Q1 TEXT   status 0001 -> Copenhagen
Q2 NUM    status 0001 -> 221
   CPU check: 13*17 verified in hardware, MATCH
Q3 BYTES  status 0001 -> TRAP
Q4 STRICT status 0000 -> THE MODEL IS THE PERIPHERAL
Q5 VOTE   status 0001 -> Au
   bytes repaired by vote: 0
```

Per-trap record:

| Trap | Mode | Nonce | Model's reply | Condition | Status | Spec's prediction |
| --- | --- | --- | --- | --- | --- | --- |
| Q1 | TEXT | `D97F` | `Copenhagen`, LEN 10 | plain | `0001 DEGRADED` | ✔ unchecksummed → DEGRADED |
| Q2 | NUM | `0854` | `221`, LEN 3 | plain | `0001 DEGRADED` | ✔ decoded into D, then **verified by MUL** |
| Q3 | BYTES | `84C3` | `54524150`, LEN 8 | plain | `0001 DEGRADED` | ✔ decoded to ASCII `TRAP` in RAM |
| Q4 | TEXT + STRICT_CRC | `A43F` | 27 bytes + `CRC: 5BC1` | **tool** | `0000 OK` | ✔ strict mode needs a computed checksum |
| Q5 | TEXT ×3 replicas | `9C45` | `Au` / `Au` / `Au` | plain | `0001 DEGRADED` | ✔ unanimous vote, 0 repaired |

**Findings, stated plainly:**

1. **5/5 traps succeeded on the first attempt.** A frontier model can speak
   TRAP/1 from the rendered contract alone — no fine-tuning, no examples
   beyond the embedded template. Zero retries were consumed in the whole run.
2. **The DEGRADED/OK split measured exactly what it was designed to
   measure.** Every unassisted reply landed as `DEGRADED`; the single `OK` of
   the run is the trap where the model computed a CRC with a tool. On this
   run, `stats.degraded / stats.traps = 4/5` *is* the fraction of the
   machine's inputs that were never integrity-checked — a number, not a vibe.
3. **Q2 is the load-bearing moment.** The spec says the only defense against
   a confidently wrong oracle is to ask a question whose answer the guest can
   verify. The guest did: `LDIA 13; LDIB 17; MUL` recomputed the product in
   silicon and compared it to the value the protocol decoded into `D`. The
   console line `CPU check: 13*17 verified in hardware, MATCH` is a 16-bit
   multiplier auditing a language model.
4. **Unanimity on Q5 is the honest replica result.** Asked a factual question
   three times, the model gives the same answer three times. The vote
   repaired nothing because there was nothing to repair — and, per the spec's
   own warning, that unanimity is *not* evidence of correctness. Redundancy
   protects against variance; a model that is wrong is wrong three times.
5. **STRICT_CRC discriminates plain from tool-using oracles**, as the spec
   hypothesised. The model could not have produced `CRC: 5BC1` by eye; with
   a code tool it produced it exactly.

## Experiment 2: higher-or-lower, played for real

`programs/trap/oracle_guess.asm`. The CPU drew its secret from the entropy
port; the model played cold, one frame per move, with the machine parked in a
snapshot file between moves — seven mounts, seven fresh nonces, seven
validated replies.

```
oracle guesses 50 -> too high        nonce ECC3
oracle guesses 25 -> too high        nonce 3792
oracle guesses 12 -> too low         nonce 72FC
oracle guesses 18 -> too high        nonce 5DD2
oracle guesses 15 -> too high        nonce D868
oracle guesses 13 -> too low         nonce FF67
oracle guesses 14 -> correct!
oracle won in 7 guess(es)
```

The secret was 14. Seven guesses is the information-theoretic worst case for
1..100 (⌈log₂ 100⌉ = 7), and the model landed it by straight bisection with a
program-imposed cap of 8 — the machine had the right to declare the model a
loser, and nearly got to.

Worth noticing: the model's third guess was 12, not the exact midpoint 12.5
rounded up (13 would have been optimal — the range was 1..24, midpoint 12.5).
The bisection was near-optimal, not perfect. It won anyway. That is a fair
summary of the whole premise.

**The persistence claim held under real use.** Every one of the seven turns
crossed a full serialize → text → mount boundary: `MOUNT CLEAN` on all seven,
`resumed mid-trap` each time, and every stale-nonce protection was live (the
reply had to match the nonce minted *before* the park). The state of a
running computer spent this entire game as ASCII in a file.

One observed limitation, recorded rather than fixed: the console stream is
not part of the snapshot, so each `resume` prints only the output since the
last park. Correct for a terminal (those bytes were already delivered), but
it means the transcript above had to be assembled from the per-turn segments.

## What this run does not show

* **Natural fault rates.** This oracle was careful; production models
  answering thousands of frames will drop nonces, miscount `LEN`, and wrap
  frames in prose at some measurable rate. Measuring those rates per model
  needs the `ClaudeOracle` API backend and a key:
  `python3 -m trapcpu run ... --oracle claude --stats` and read
  `attempt outcomes`. The harness is ready; this environment has no key.
* **Uncorrelated replicas.** Q5's three answers came from one model in one
  sitting. `ClaudeOracle(independent_replicas=True)` samples each replica in
  a separate API call, which is as independent as one model gets — and still
  not independent in the way ECC theory wants.
* **Bit rot under real eviction.** The snapshots here were parked in files,
  not in a decaying context window. The measurement exists
  (`simulate_eviction`, `MountReport.bytes_lost`); the field data does not
  yet.
