# BCIF speed optimization experiments

## Purpose

This branch isolates experiments intended to reduce the serialization overhead
of the current BinaryCIF float-chain PR without weakening compression or
correctness guarantees.

The benchmark accepts an exact optimization only when every tested output is:

- byte-identical to the current PR output;
- identical after typed decoding;
- unchanged in total size;
- accepted by the ciftools-java encoding verifier.

## Full 37-structure result

Reference commit: `e1037d385afb5a222b67e8b5a9167abf6ab8e1b7`

| Variant | Sum of per-structure medians | Faster vs PR | Total bytes | Byte identical | Semantic identical | Java |
|---|---:|---:|---:|:---:|:---:|:---:|
| `all_exact` | 104.113538 s | 11.314% | 78,867,022 | yes | yes | pass |
| `all_exact_repeat` | 105.083592 s | 10.487% | 78,867,022 | yes | yes | pass |
| `pr_current` | 117.395278 s | baseline | 78,867,022 | yes | yes | pass |

`all_exact` is the leading experimental stack. This result validates the
combined stack, not yet every individual optimization in isolation.

## Dictionary policy

The corpus follows the mentor-directed configuration:

- PDB: base mmCIF dictionary;
- IHM: base + IHM extension;
- ModelCIF/CSM: base + ModelCIF extension;
- FLR extension intentionally excluded.

Unknown FLR attributes therefore intentionally fall back to strings and produce
warnings.

## Why the harness is streaming

The original full-corpus harness retained clone caches for all structures.
`3J3Q.cif` is approximately 231 MB and expands into a much larger Python object
graph, exhausting the available WSL memory during corpus preparation.

The streaming harness keeps one structure resident at a time and promptly
removes temporary outputs.

## Included files

- `benchmarks/scripts/benchmark_bcif_speed_optimizations_streaming.py`
- `benchmarks/scripts/run_bcif_full_confirmation_streaming.sh`
- `benchmarks/data/bcif_speed_manifest_full.tsv`
- `benchmarks/evidence/bcif_speed_full_confirmation_e1037d3/`

The CIF corpus, dictionaries, Java verifier JAR, generated BCIF outputs, and
large console log are intentionally not committed.

## Running the benchmark

The manifest uses repository-relative paths. Populate the corresponding local
CIF and dictionary paths, then run:

```bash
export JAVA_VERIFIER=/absolute/path/to/encoding-verifier.jar
benchmarks/scripts/run_bcif_full_confirmation_streaming.sh
```

Optional environment variables:

- `WARMUPS` — default `1`
- `RUNS` — default `7`
- `VARIANTS` — default `pr_current,all_exact,all_exact_repeat`
- `BCIF_MANIFEST`
- `BCIF_RESULTS_DIR`
- `BCIF_TIMING_ROOT`
- `BCIF_LOG`

## Next engineering step

Decompose `all_exact`, select the smallest production-worthy optimization set,
implement it directly in `BinaryCifWriter.py`, and repeat the full Python,
byte-identity, semantic-identity, size, and Java-verifier checks.
