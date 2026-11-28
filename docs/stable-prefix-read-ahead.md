# Stable-prefix read-ahead preparation

WP-E6 remains disabled in production. `lab/stable_prefix_replay.py` is a
model-independent planner for synthetic recognition replays; it does not open
a microphone, recognizer or Cortex endpoint.

Each event supplies the tokenizer-under-test's original nonnegative bounded
token IDs and their exact Python character spans. Spans must be ordered,
non-overlapping and cover every non-whitespace input character; this schema
does not pretend to represent a tokenizer whose exact offsets overlap. The
planner never reconstructs or retokenizes them. A prefix becomes eligible
only when complete whitespace words are unchanged across two consecutive
partials, both observations give the identical token-ID prefix, that prefix
extends the active checkpoint byte-for-byte, and at least eight new tokens can
be committed. Every commit creates a checkpoint. A revision that no longer
extends the active token sequence rolls back to the newest compatible
checkpoint; the final transcript receives the same compatibility check.

Input kind `pipos-stable-prefix-replay-v1` contains measured descriptive
`prefill_tokens_per_second`, bounded `request_overhead_ms`, measured bounded
`rollback_overhead_ms`, and explicit
`provenance`. Checked-in data uses `synthetic_fixture` and a tokenizer label;
a future owner-consented native replay must say `consented_native_replay` and
bind the tokenizer binary/model SHA-256. It contains 1–100 turns whose
`synthetic` flag must agree with that provenance. Each turn
has a bounded non-private ID, an optional D4 `scaffold_n`, the acoustic
`speech_end_ms` on the same timeline as the recognition events, the measured
non-speculative `baseline_eos_to_first_token_ms`, and 2–256 increasing events:

```json
{"kind":"partial","revision":1,"at_ms":120,
 "text":"please",
 "tokens":[{"id":101,"start":0,"end":6}]}
```

The token array must contain every actual token required by the qualified
prompt boundary, with non-overlapping spans. A final event must be last. Run
later with:

```sh
python3 lab/stable_prefix_replay.py synthetic-replay.json > plan.json
```

`lab/fixtures/stable-prefix-synthetic.json` is a deliberately content-free
fixture covering one retained commit and one corrected final that rolls its
commit back.

Requests are simulated serially. A candidate pays the supplied overhead plus
`new_tokens / measured_rate` and counts as committed only if it finishes
before the next recognition revision or acoustic end of speech. Later commits and rollbacks bind
to the current checkpoint ancestry. A rollback occupies the same serial lane
for its supplied duration. The projected penalty includes only work that
still blocks generation after the final transcript arrives;
unfinished work contributes no saving.

Per-turn output includes `scaffold_n`, total `speculative_n`, tokens discarded
as `rolled_back_n`, final compatible prefix, `tail_n`, checkpoint high-water,
rollback trace, attempted request durations, and projected timing derived from
the supplied measured rate. The report applies the handover's descriptive 0.4-second, one-rollback
and six-checkpoint warnings, but both `native_acceptance` and
`production_prefill` are always false. A later native trial must demonstrate
greedy reply identity and real `eos_to_first_token`; this arithmetic planner
cannot establish either.
