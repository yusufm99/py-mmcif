#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

mkdir -p benchmarks/data/cif

while read -r id; do
  [[ -z "$id" || "$id" == \#* ]] && continue

  lower=$(echo "$id" | tr '[:upper:]' '[:lower:]')
  out="benchmarks/data/cif/${id}.cif"

  echo "Downloading PDB $id -> $out"

  curl -L \
    "https://files.rcsb.org/download/${lower}.cif" \
    -o "$out"

done < benchmarks/targets_pdb.txt
