# BinaryCIF speed-optimization benchmark report

Generated: 2026-06-25T14:25:32.331502+00:00
Script version: `1.0.2-streaming`
Repository commit: `e1037d385afb5a222b67e8b5a9167abf6ab8e1b7`

## Decision rule

A method is considered safe only when every tested file is byte-identical to the current PR output, decodes to the same typed values, and passes the optional Java verifier. Tradeoff methods are ranked separately.

## Aggregate results

| Variant | Sum of medians (s) | Faster vs PR | Total bytes | Size change vs PR | Byte-identical | Semantic-identical | Java |
|---|---:|---:|---:|---:|:---:|:---:|:---:|
| `all_exact` | 104.113538 | +11.314% | 78,867,022 | +0.000000% | yes | yes | pass |
| `all_exact_repeat` | 105.083592 | +10.487% | 78,867,022 | +0.000000% | yes | yes | pass |
| `pr_current` | 117.395278 | +0.000% | 78,867,022 | +0.000000% | yes | yes | pass |

## Best verified no-regression method

`all_exact` is the fastest strategy that met every configured no-regression check. It was 11.314% faster than the current PR across the aggregate corpus, with 0.000000% aggregate size change.

## Tradeoff methods

No tradeoff methods were selected.

## Pareto frontier

`all_exact`

## Variant definitions

- `pr_current` (expected byte-identical): Unmodified current PR: fully encode all four general FixedPoint candidates.
- `all_exact` (expected byte-identical): Combine all exact optimizations with array.array ByteArray output.
- `all_exact_repeat` (expected byte-identical): Combine all exact optimizations with compact repeated-struct ByteArray output.

## Evidence files

- `runs.tsv`: every measured repetition.
- `summary.tsv`: per-structure medians, variability, size, and validation.
- `aggregate.tsv`: corpus-level totals and comparisons.
- `validation.tsv`: SHA-256, decoded digest, and Java-verifier result.
- `variants.tsv`: strategy definitions and expected guarantees.
- `metadata.json`: environment, source hash, Git state, and arguments.
- `profiles/`: optional cProfile output.
- `memory.tsv`: optional tracemalloc measurements.
