# Ablation: which model calls actually mattered

```console
$ python3 -m trapcpu ablate programs/trap/oracle_sort.asm
```

Three questions about any TRAPCPU program, answered exactly rather than
statistically, because nothing in this machine except the trap is
nondeterministic.

## What it measures

**Criticality.** Force call *k* to a different answer, let the oracle answer
everything downstream as normal, and see whether the program's output changes.
Repeat for every call.

**Placebo.** Replace the oracle entirely with uniform noise over the answers it
actually gave. If the output barely moves, the model was not doing the work.

**Self correction.** Replace only the *first k* calls with noise and sweep k.
This asks how much of the oracle you could simply not pay for.

## What it found in `oracle_sort.asm`

```
CRITICALITY  (force one answer, let the rest run normally)
  load bearing : 4/16
  no effect    : 12/16
    call   0  ans='1'    --     max 0 lines moved
    ...
    call  12  ans='2'  CHANGED  max 2 lines moved
    call  13  ans='2'  CHANGED  max 2 lines moved
    call  14  ans='2'  CHANGED  max 2 lines moved
    call  15  ans='2'  CHANGED  max 2 lines moved

SELF CORRECTION  (first k calls replaced by noise, 40 trials each)
    first   0 random  100%  ############################
    first   1 random  100%  ############################
    first   2 random  100%  ############################
    first   3 random  100%  ############################
    first   4 random   95%  ###########################
    first   6 random   95%  ###########################
    first   8 random   57%  #################
    first  12 random   12%  ####
    first  15 random    0%
```

Twelve of the sixteen model calls can be individually corrupted with no effect
on the output whatsoever. Only the final four are load-bearing.

The reason is structural, and it is the interesting part. Bubble sort makes
four passes of four comparisons. A wrong answer in passes one through three
gets *repaired* by a later pass that re-compares the same items. The final pass
has nothing after it, so its four judgments are unprotected.

**The algorithm is error correction for the model.** Not by design - nobody
added redundancy for this - but as a side effect of an ordinary sort revisiting
its own decisions. The first three calls can be literal coin flips and the sort
is still exactly right, every time.

That generalises past this toy. Any scaffold that revisits its decisions
absorbs some rate of model error for free, and the ablation tells you exactly
where the absorption stops. If the last four calls are the only ones that
matter, that is where a better model earns its cost - and the other twelve are
where the budget is going to waste.

## Prior art, stated up front

The criticality measurement is not new. Recording the nondeterministic model
calls, intervening on one, and re-executing the deterministic remainder is the
record-replay counterfactual discipline, and it is worked out properly in:

- [Causal Agent Replay: Counterfactual Attribution for LLM-Agent Failures](https://arxiv.org/abs/2606.08275)
  — models the run as a structural causal model, applies a `do` operation to a
  step, re-executes forward, and measures the shift in outcome distribution.
- [REFLECT: Intervention-Supported Error Attribution for Silent Failures in LLM Agent Traces](https://arxiv.org/html/2606.09071v1)
  — localises the earliest decisive error by injecting a correction and
  replaying from that point.
- [Contextual Counterfactual Credit Assignment for Multi-Agent LLM Collaboration](https://arxiv.org/html/2603.06859v1)
  — repeatable ablations against a fixed transcript-derived context.

The placebo is an ordinary random baseline, which is standard practice in
machine learning generally and conspicuously rare in LLM pipelines specifically.

What this implementation adds is not method but **exactness**. Those papers
fight to get a deterministic substrate out of real agent traces and settle for
distributional answers. Here determinism is free: the intervention is the only
thing that changed, so a difference in output *is* the causal effect, with no
variance to average away. That makes this a clean rig for demonstrating those
techniques, not a new one.

## Caveats

- Counterfactual exactness requires an oracle that is a function of its prompt.
  With `--oracle claude` the model itself is stochastic and the numbers become
  a sample like everyone else's.
- The noise curve is sampled; low `--trials` makes it visibly non-monotonic.
  Raise it before reading anything into a single point.
- `--oracle judge` holds one hardcoded opinion. Findings about *this* comparator
  are findings about a lookup table, not about a model. The structural result -
  which calls are load-bearing - is a property of the program and holds either
  way.
