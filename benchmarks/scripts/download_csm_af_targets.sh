#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

mkdir -p benchmarks/data/cif

echo "Downloading AlphaFold CSM mmCIF files"

curl -L \
  "https://alphafold.ebi.ac.uk/files/AF-A0A017SEY2-F1-model_v6.cif" \
  -o "benchmarks/data/cif/AF_AFA0A017SEY2F1.cif"

curl -L \
  "https://alphafold.ebi.ac.uk/files/AF-A0A017SQ41-F1-model_v6.cif" \
  -o "benchmarks/data/cif/AF_AFA0A017SQ41F1.cif"
