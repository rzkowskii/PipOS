# Extract audition preparation

Extract remains a separate acceptance arm (task 665) from the Compose blind
audition. `lab/extract-audition-cases.json` contains ten wholly synthetic
notes, list and dated-conversation cases, including corrected facts, competing
deadlines, a newer status replacing an older one, and an intentionally empty
trivia case. Each case has hand-authored permissible index sets; no private
archive is read.

With the shipped dream model already running on its normal loopback endpoint:

```sh
python3 lab/extract_audition.py \
  --output out/extract-audition/results.json
```

The runner calls `mind.ponder._request()` to build the exact schema-constrained
production prompt and `mind.ponder._picks()` to validate the returned indices.
It does not substitute a fake model, start a service, or write to the mind.
Only numeric loopback addresses are accepted. Each request is bounded to 30
seconds by default; `--timeout` may be lowered but not raised above the
acceptance bound.

The JSON result records selected indices, exact membership in a permissible
set, the closest permissible set, coverage, missing and unsupported indices,
the local server's token counts, and per-case/aggregate wall time. The output
must be new so an earlier audition is not overwritten. This runner measures
Extract only and does not promote Compose or enable Build.
