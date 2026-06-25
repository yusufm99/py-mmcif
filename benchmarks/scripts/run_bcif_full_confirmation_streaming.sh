#!/usr/bin/env bash
set -euo pipefail
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"

SCRIPT="$REPO/benchmarks/scripts/benchmark_bcif_speed_optimizations_streaming.py"
MANIFEST="${BCIF_MANIFEST:-$REPO/benchmarks/data/bcif_speed_manifest_full.tsv}"
RESULTS="${BCIF_RESULTS_DIR:-$REPO/benchmarks/results/bcif_speed_full_confirmation_streaming}"
TIMING="${BCIF_TIMING_ROOT:-$REPO/benchmarks/results/bcif_speed_streaming_tmp}"
LOG="${BCIF_LOG:-$REPO/benchmarks/results/bcif_speed_full_confirmation_streaming_console.log}"

VERIFIER="${JAVA_VERIFIER:-}"
WARMUPS="${WARMUPS:-1}"
RUNS="${RUNS:-7}"
VARIANTS="${VARIANTS:-pr_current,all_exact,all_exact_repeat}"

cd "$REPO"

if [[ -z "$VERIFIER" ]]; then
  cat >&2 <<'USAGE'
ERROR: JAVA_VERIFIER is not set.

Example:
  export JAVA_VERIFIER=/absolute/path/to/encoding-verifier.jar
  benchmarks/scripts/run_bcif_full_confirmation_streaming.sh
USAGE
  exit 2
fi

for required in "$SCRIPT" "$MANIFEST" "$VERIFIER"; do
  if [[ ! -f "$required" ]]; then
    echo "ERROR: required file not found: $required" >&2
    exit 1
  fi
done

rm -rf "$RESULTS" "$TIMING"
rm -f "$LOG"
mkdir -p "$TIMING" "$(dirname "$LOG")"

python -u "$SCRIPT" \
  --repo "$REPO" \
  --manifest "$MANIFEST" \
  --results-dir "$RESULTS" \
  --timing-root "$TIMING" \
  --warmups "$WARMUPS" \
  --runs "$RUNS" \
  --variants "$VARIANTS" \
  --java-verifier "$VERIFIER" \
  2>&1 | tee "$LOG"

status=${PIPESTATUS[0]}
rm -rf "$TIMING"

echo
echo "Benchmark exit code: $status"
echo "Report: $RESULTS/report.md"
echo "Log:    $LOG"
exit "$status"
