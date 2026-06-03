#!/usr/bin/env python3
"""
Minimal py-mmcif BCIF benchmark script.

Records only the benchmark data requested in the project plan:

1. Time for encoding, with and without GZIP
2. Time for encoding, with and without input dictionary
3. Data compression efficiency, as percent of original CIF size
4. Time for decoding, from GZIP and unzipped BCIF

Run from the py-mmcif repo root.
"""

import argparse
import csv
import gzip
import shutil
import time
from pathlib import Path

from mmcif.io.PdbxReader import PdbxReader
from mmcif.io.BinaryCifReader import BinaryCifReader
from mmcif.io.BinaryCifWriter import BinaryCifWriter
from mmcif.io.IoAdapterPy import IoAdapterPy
from mmcif.api.DictionaryApi import DictionaryApi


DEFAULT_PDB_IDS = [
    "4HHB", "2LGI", "3HQV", "7ART", "3J3Q", "4BTS", "11BJ", "11HB",
    "10DK", "12GB", "11TG", "9O5G", "9QW5", "6VC1", "3PDM", "2XKM",
    "9HH6", "3IFX", "1RMN", "1SSZ", "2BVK", "1UR6",
]


def seconds():
    return time.perf_counter()


def file_size_bytes(path):
    path = Path(path)
    return path.stat().st_size if path.exists() else ""


def read_cif(cif_path):
    """Read text mmCIF into py-mmcif containers.

    This is intentionally NOT timed/recorded because the requested benchmark
    metrics are about BCIF encoding/decoding, not CIF parsing.
    """
    containers = []
    with open(cif_path, "r", encoding="utf-8", errors="replace") as handle:
        reader = PdbxReader(handle)
        reader.read(containers)
    return containers


def load_dictionary_api(dict_path):
    """Load a dictionary file and return a DictionaryApi object."""
    if not dict_path:
        return None

    dict_path = Path(dict_path)
    if not dict_path.exists():
        raise FileNotFoundError(f"Dictionary file not found: {dict_path}")

    io = IoAdapterPy(raiseExceptions=True)
    dictionary_containers = io.readFile(inputFilePath=str(dict_path))
    return DictionaryApi(containerList=dictionary_containers, consolidate=True)


def encode_plain_bcif(containers, output_path, dictionary_api, use_dictionary):
    """Encode containers to an uncompressed .bcif file."""

    writer = BinaryCifWriter(
        dictionaryApi=dictionary_api,
        applyTypes=use_dictionary,
        useStringTypes=not use_dictionary,
        ignoreCastErrors=True,
    )

    ok = writer.serialize(str(output_path), containers)
    if not ok or not Path(output_path).exists():
        raise RuntimeError(f"BCIF serialization failed: {output_path}")


def gzip_file(input_path, output_path):
    """Compress an existing BCIF file to .bcif.gz."""
    with open(input_path, "rb") as source:
        with gzip.open(output_path, "wb") as dest:
            shutil.copyfileobj(source, dest)


def decode_bcif(input_path):
    """Decode either .bcif or .bcif.gz."""
    reader = BinaryCifReader(storeStringsAsBytes=False)
    return reader.deserialize(str(input_path))


def benchmark_target(
    target_id,
    target_type,
    cif_path,
    plain_dir,
    gz_dir,
    dictionary_mode,
    dictionary_api,
):
    """Benchmark one target in one dictionary mode.

    One output row represents target + dictionary mode.
    """
    use_dictionary = dictionary_mode == "with_dictionary"

    plain_dir = Path(plain_dir)
    gz_dir = Path(gz_dir)
    plain_dir.mkdir(parents=True, exist_ok=True)
    gz_dir.mkdir(parents=True, exist_ok=True)

    plain_bcif = plain_dir / f"{target_id}.{dictionary_mode}.bcif"
    gzip_temp_plain_bcif = plain_dir / f"{target_id}.{dictionary_mode}.for_gzip.tmp.bcif"
    gz_bcif = gz_dir / f"{target_id}.{dictionary_mode}.bcif.gz"

    row = {
        "target_id": target_id,
        "target_type": target_type,
        "implementation": "py-mmcif",
        "dictionary_mode": dictionary_mode,
        "read_cif_seconds": "",
        "cif_size_bytes": "",
        "bcif_size_bytes": "",
        "bcif_gz_size_bytes": "",
        "encode_without_gzip_seconds": "",
        "encode_with_gzip_seconds": "",
        "bcif_percent_of_cif": "",
        "bcif_gz_percent_of_cif": "",
        "decode_from_unzipped_seconds": "",
        "decode_from_gzip_seconds": "",
    }

    cif_path = Path(cif_path)
    if not cif_path.exists():
        raise FileNotFoundError(f"Missing CIF file: {cif_path}")

    # 0. Read CIF file into py-mmcif containers.
    # This measures local file read + CIF parsing time.
    start = seconds()
    containers = read_cif(cif_path)
    row["read_cif_seconds"] = seconds() - start

    row["cif_size_bytes"] = file_size_bytes(cif_path)

    # 1. Encode WITHOUT gzip: containers -> .bcif
    start = seconds()
    encode_plain_bcif(
        containers=containers,
        output_path=plain_bcif,
        dictionary_api=dictionary_api,
        use_dictionary=use_dictionary,
    )
    row["encode_without_gzip_seconds"] = seconds() - start
    row["bcif_size_bytes"] = file_size_bytes(plain_bcif)

    # 2. Encode WITH gzip: containers -> temp .bcif -> .bcif.gz
    # Timed separately so gzip mode is measured as a full export path.
    start = seconds()
    encode_plain_bcif(
        containers=containers,
        output_path=gzip_temp_plain_bcif,
        dictionary_api=dictionary_api,
        use_dictionary=use_dictionary,
    )
    gzip_file(gzip_temp_plain_bcif, gz_bcif)
    row["encode_with_gzip_seconds"] = seconds() - start
    row["bcif_gz_size_bytes"] = file_size_bytes(gz_bcif)

    try:
        gzip_temp_plain_bcif.unlink()
    except FileNotFoundError:
        pass

    # 3. Compression efficiency
    cif_size = row["cif_size_bytes"]
    bcif_size = row["bcif_size_bytes"]
    bcif_gz_size = row["bcif_gz_size_bytes"]

    if cif_size:
        row["bcif_percent_of_cif"] = (bcif_size / cif_size) * 100.0
        row["bcif_gz_percent_of_cif"] = (bcif_gz_size / cif_size) * 100.0

    # 4. Decode from unzipped .bcif
    start = seconds()
    decode_bcif(plain_bcif)
    row["decode_from_unzipped_seconds"] = seconds() - start

    # 5. Decode from gzipped .bcif.gz
    start = seconds()
    decode_bcif(gz_bcif)
    row["decode_from_gzip_seconds"] = seconds() - start

    return row


def read_target_file(path, default_target_type="PDB"):
    """Read target IDs from a simple text file.

    Format:
      4HHB
      2LGI

    Blank lines and comments starting with # are ignored.
    """
    targets = []
    path = Path(path)
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            targets.append((clean, default_target_type))
    return targets


def write_csv(rows, output_csv):
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "target_id",
        "target_type",
        "implementation",
        "dictionary_mode",
        "read_cif_seconds",
        "cif_size_bytes",
        "bcif_size_bytes",
        "bcif_gz_size_bytes",
        "encode_without_gzip_seconds",
        "encode_with_gzip_seconds",
        "bcif_percent_of_cif",
        "bcif_gz_percent_of_cif",
        "decode_from_unzipped_seconds",
        "decode_from_gzip_seconds",
    ]

    with open(output_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cif-dir", default="benchmarks/data/cif")
    parser.add_argument("--plain-dir", default="benchmarks/data/bcif_plain")
    parser.add_argument("--gz-dir", default="benchmarks/data/bcif_gz")
    parser.add_argument("--output-csv", default="benchmarks/results/pymmcif_minimal_benchmark.csv")

    parser.add_argument("--targets", nargs="*", default=None)
    parser.add_argument("--targets-file", default=None)
    parser.add_argument("--target-type", default="PDB")

    parser.add_argument(
        "--modes",
        nargs="*",
        default=["no_dictionary", "with_dictionary"],
        choices=["no_dictionary", "with_dictionary"],
    )
    parser.add_argument("--dict-path", default=None)

    args = parser.parse_args()

    if args.targets_file:
        targets = read_target_file(args.targets_file, default_target_type=args.target_type)
    elif args.targets:
        targets = [(target_id, args.target_type) for target_id in args.targets]
    else:
        targets = [(target_id, "PDB") for target_id in DEFAULT_PDB_IDS]

    dictionary_api = None
    if "with_dictionary" in args.modes:
        if not args.dict_path:
            raise ValueError(
                "You requested with_dictionary mode but did not provide --dict-path."
            )
        dictionary_api = load_dictionary_api(args.dict_path)

    rows = []

    for target_id, target_type in targets:
        cif_path = Path(args.cif_dir) / f"{target_id}.cif"

        for dictionary_mode in args.modes:
            print(f"Benchmarking {target_id} [{dictionary_mode}]")

            row = benchmark_target(
                target_id=target_id,
                target_type=target_type,
                cif_path=cif_path,
                plain_dir=args.plain_dir,
                gz_dir=args.gz_dir,
                dictionary_mode=dictionary_mode,
                dictionary_api=dictionary_api if dictionary_mode == "with_dictionary" else None,
            )
            rows.append(row)

    write_csv(rows, args.output_csv)
    print(f"Wrote: {args.output_csv}")


if __name__ == "__main__":
    main()
