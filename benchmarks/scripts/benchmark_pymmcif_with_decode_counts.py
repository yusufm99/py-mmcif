#!/usr/bin/env python3
"""
py-mmcif BCIF benchmark script with Hriday-style Python decoding metrics.

Adds these columns:
- num_blocks
- num_categories
- total_rows
- pipeline_runtime_ms
- decoding_runtime_ms

These are measured from the unzipped .bcif file.
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
    containers = []
    with open(cif_path, "r", encoding="utf-8", errors="replace") as handle:
        reader = PdbxReader(handle)
        reader.read(containers)
    return containers


def load_dictionary_api(dict_path):
    if not dict_path:
        return None

    dict_path = Path(dict_path)
    if not dict_path.exists():
        raise FileNotFoundError(f"Dictionary file not found: {dict_path}")

    io = IoAdapterPy(raiseExceptions=True)
    dictionary_containers = io.readFile(inputFilePath=str(dict_path))
    return DictionaryApi(containerList=dictionary_containers, consolidate=True)


def encode_plain_bcif(containers, output_path, dictionary_api, use_dictionary):
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
    with open(input_path, "rb") as source:
        with gzip.open(output_path, "wb") as dest:
            shutil.copyfileobj(source, dest)


def summarize_decoded_containers(containers):
    """
    Count decoded BCIF structure contents.

    num_blocks = number of data blocks
    num_categories = total number of categories across all blocks
    total_rows = total rows across all categories
    """
    num_blocks = len(containers)
    num_categories = 0
    total_rows = 0

    for container in containers:
        obj_names = container.getObjNameList()
        num_categories += len(obj_names)

        for obj_name in obj_names:
            obj = container.getObj(obj_name)
            if obj is not None and hasattr(obj, "getRowCount"):
                total_rows += obj.getRowCount()

    return {
        "num_blocks": num_blocks,
        "num_categories": num_categories,
        "total_rows": total_rows,
    }


def decode_bcif_with_metrics(input_path):
    """
    Decode .bcif or .bcif.gz and return timing + decoded structure counts.

    pipeline_runtime_ms:
        total time for decode call + counting decoded blocks/categories/rows

    decoding_runtime_ms:
        time for BinaryCifReader.deserialize()

    Note:
        py-mmcif does not expose a separate public timer for only the internal
        msgpack-to-category conversion, so decoding_runtime_ms is the public
        deserialize() call timing.
    """
    reader = BinaryCifReader(storeStringsAsBytes=False)

    pipeline_start = seconds()

    decode_start = seconds()
    containers = reader.deserialize(str(input_path))
    decoding_runtime_ms = (seconds() - decode_start) * 1000.0

    stats = summarize_decoded_containers(containers)

    pipeline_runtime_ms = (seconds() - pipeline_start) * 1000.0

    return {
        "containers": containers,
        "num_blocks": stats["num_blocks"],
        "num_categories": stats["num_categories"],
        "total_rows": stats["total_rows"],
        "pipeline_runtime_ms": pipeline_runtime_ms,
        "decoding_runtime_ms": decoding_runtime_ms,
    }


def benchmark_target(
    target_id,
    target_type,
    cif_path,
    plain_dir,
    gz_dir,
    dictionary_mode,
    dictionary_api,
):
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

        # Hriday-style Python decoding metrics from unzipped BCIF
        "num_blocks": "",
        "num_categories": "",
        "total_rows": "",
        "pipeline_runtime_ms": "",
        "decoding_runtime_ms": "",
    }

    cif_path = Path(cif_path)
    if not cif_path.exists():
        raise FileNotFoundError(f"Missing CIF file: {cif_path}")

    # 0. Read CIF file into py-mmcif containers
    start = seconds()
    containers = read_cif(cif_path)
    row["read_cif_seconds"] = seconds() - start

    row["cif_size_bytes"] = file_size_bytes(cif_path)

    # 1. Encode WITHOUT gzip
    start = seconds()
    encode_plain_bcif(
        containers=containers,
        output_path=plain_bcif,
        dictionary_api=dictionary_api,
        use_dictionary=use_dictionary,
    )
    row["encode_without_gzip_seconds"] = seconds() - start
    row["bcif_size_bytes"] = file_size_bytes(plain_bcif)

    # 2. Encode WITH gzip
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

    # 4. Decode from unzipped .bcif and collect Hriday-style metrics
    unzipped_decode = decode_bcif_with_metrics(plain_bcif)

    row["decode_from_unzipped_seconds"] = unzipped_decode["decoding_runtime_ms"] / 1000.0
    row["num_blocks"] = unzipped_decode["num_blocks"]
    row["num_categories"] = unzipped_decode["num_categories"]
    row["total_rows"] = unzipped_decode["total_rows"]
    row["pipeline_runtime_ms"] = unzipped_decode["pipeline_runtime_ms"]
    row["decoding_runtime_ms"] = unzipped_decode["decoding_runtime_ms"]

    # 5. Decode from gzipped .bcif.gz
    gz_decode = decode_bcif_with_metrics(gz_bcif)
    row["decode_from_gzip_seconds"] = gz_decode["decoding_runtime_ms"] / 1000.0

    return row


def read_target_file(path, default_target_type="PDB"):
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

        # New Hriday-style Python decoding metrics
        "num_blocks",
        "num_categories",
        "total_rows",
        "pipeline_runtime_ms",
        "decoding_runtime_ms",
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
    parser.add_argument("--output-csv", default="benchmarks/results/pymmcif_benchmark_with_decode_counts.csv")

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
            raise ValueError("You requested with_dictionary mode but did not provide --dict-path.")
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