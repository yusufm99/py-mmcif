#!/usr/bin/env python3
"""Benchmark BinaryCIF writer speed optimizations without hiding size/accuracy tradeoffs.

This harness is intended for the current py-mmcif PR that adds FixedPoint float
encoding and a four-chain selector.  It does not edit the repository.  Instead,
it applies each strategy as a temporary in-process monkey patch, writes real BCIF
files, measures BinaryCifWriter.serialize(), and validates every strategy against
the current PR output.

Primary guarantees checked by this script
-----------------------------------------
* file size and SHA-256 for every generated BCIF;
* decoded semantic equality against the current PR output;
* optional byte-for-byte equality for strategies expected to be exact;
* optional ciftools-java encoding-verifier execution;
* median wall time, median CPU time, MAD, aggregate totals, and Pareto frontier;
* optional cProfile and tracemalloc diagnostics;
* complete TSV/JSON/Markdown evidence package.

The safest/highest-value strategies included are:
1. short_exact
   For columns with <= 40 values, evaluate only the first chain.  Delta and
   RunLength are no-ops at this size in the current writer, so all four candidates
   are identical.
2. selector_cached_exact
   Convert FixedPoint once, construct reusable Delta/RunLength transforms once,
   predict the exact final IntegerPacking byte length, then fully encode only the
   winning candidate.  This preserves the current four-way choice and tie order.
3. float_pipeline_exact
   Extends (2) to specialized coordinate/anisotrop/radius/run-length paths so the
   writer does not scale the same float column once for safety and again to encode.
4. fixedpoint_one_pass_exact / numeric_hotloops_exact
   Fuse FixedPoint conversion and Int32 validation into one pass, preallocate
   Delta output, and stop unhelpful RunLength work as soon as expansion is certain.
5. bytearray_repeat_exact / bytearray_array_exact
   Avoid the very large repeated struct-format string, or use array.array with
   explicit little-endian handling.
6. fast_integer_pack_exact
   Preserve IntegerPacking output while reducing repeated scans and while-loops.
7. reuse_encoder_exact
   Reuse one stateless BinaryCifEncoders instance across columns in a writer.
8. all_exact / all_exact_repeat
   Combine the exact optimizations above using two ByteArray implementations so
   the benchmark, rather than an assumption, selects the faster final stack.

Tradeoff strategies are also included to answer the reviewer question directly:
best2_empirical, single_ip, single_rle, single_delta, and single_delta_rle.
They preserve decoded values but are not guaranteed to preserve the smallest
payload on unseen columns.

Run ``python benchmark_bcif_speed_optimizations.py --help`` for usage.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import copy
import cProfile
import csv
import dataclasses
import datetime as dt
import gc
import gzip
import hashlib
import importlib
import io
import itertools
import json
import math
import os
import pickle
import platform
import random
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import tracemalloc
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence


SCRIPT_VERSION = "1.0.2-streaming"
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


@dataclasses.dataclass(frozen=True)
class Entry:
    structure_id: str
    cif_path: Path
    dictionary_paths: tuple[Path, ...]
    kind: str = ""


@dataclasses.dataclass(frozen=True)
class Variant:
    name: str
    description: str
    expected_byte_identical: bool
    expected_semantic_identical: bool
    tradeoff: bool
    apply: Callable[[Any, Mapping[str, Any]], None]


@dataclasses.dataclass
class RunResult:
    structure_id: str
    kind: str
    variant: str
    phase: str
    repetition: int
    order_index: int
    wall_seconds: float
    cpu_seconds: float
    size_bytes: int
    sha256: str
    success: bool
    error: str = ""


class ContainerProvider:
    """Provide fresh containers while keeping only one structure resident.

    The original benchmark retained clone caches for every manifest entry. A large
    text mmCIF such as 3J3Q can expand to several gigabytes of Python objects, so
    retaining all prior entries and an in-memory pickle can exhaust an 8-GiB WSL
    VM before timing begins. This provider writes one entry's clone cache directly
    to disk, releases the parsed master, and is destroyed before the next entry.
    """

    def __init__(self, path: Path, reader: Callable[[Path], list[Any]], cache_path: Path):
        self.path = path
        self.reader = reader
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._use_pickle = False

        master: list[Any] | None = reader(path)
        try:
            with self.cache_path.open("wb") as fh:
                pickle.dump(master, fh, protocol=pickle.HIGHEST_PROTOCOL)
            self._use_pickle = True
        except Exception:
            self.cache_path.unlink(missing_ok=True)
            self._use_pickle = False
        finally:
            del master
            gc.collect()

    def clone(self) -> list[Any]:
        if self._use_pickle:
            with self.cache_path.open("rb") as fh:
                return pickle.load(fh)
        return self.reader(self.path)

    def cleanup(self) -> None:
        self.cache_path.unlink(missing_ok=True)


class PatchManager:
    """Reset writer and encoder methods before applying each strategy."""

    ENCODER_METHOD_NAMES = (
        "_BinaryCifEncoders__encodeBestFixedPointChain",
        "_BinaryCifEncoders__encodeFixedPointChainOrFallback",
        "floatArrayMaskedEncoder",
        "fixedPointEncoderTyped",
        "deltaEncoderTyped",
        "runLengthEncoderTyped",
        "byteArrayEncoderTyped",
        "_determine_packing",
        "integerPackingEncoderTyped",
    )
    WRITER_METHOD_NAMES = ("_BinaryCifWriter__encodeColumnData",)

    def __init__(self, module: Any):
        self.module = module
        self.encoder_cls = module.BinaryCifEncoders
        self.writer_cls = module.BinaryCifWriter
        self.encoder_originals = {name: getattr(self.encoder_cls, name) for name in self.ENCODER_METHOD_NAMES}
        self.writer_originals = {name: getattr(self.writer_cls, name) for name in self.WRITER_METHOD_NAMES}
        self.originals = {**self.encoder_originals, **self.writer_originals}
        self.original_global_encoder = module.BinaryCifEncoders
        self.original_global_writer = module.BinaryCifWriter

    def reset(self) -> None:
        self.module.BinaryCifEncoders = self.original_global_encoder
        self.module.BinaryCifWriter = self.original_global_writer
        self.encoder_cls = self.original_global_encoder
        self.writer_cls = self.original_global_writer
        for name, value in self.encoder_originals.items():
            setattr(self.encoder_cls, name, value)
        for name, value in self.writer_originals.items():
            setattr(self.writer_cls, name, value)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def median_abs_deviation(values: Sequence[float]) -> float:
    if not values:
        return math.nan
    med = statistics.median(values)
    return statistics.median(abs(v - med) for v in values)


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return math.nan
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return data[lo]
    frac = pos - lo
    return data[lo] * (1.0 - frac) + data[hi] * frac


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return math.nan
    return 100.0 * (new - old) / old


def pct_faster(new: float, old: float) -> float:
    if old == 0:
        return math.nan
    return 100.0 * (old - new) / old


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)


def open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def read_cif(path: Path) -> list[Any]:
    from mmcif.io.PdbxReader import PdbxReader

    containers: list[Any] = []
    with open_text(path) as fh:
        PdbxReader(fh).read(containers)
    if not containers:
        raise RuntimeError(f"No data containers parsed from {path}")
    return containers


def load_dictionary_api(paths: Sequence[Path]) -> Any:
    from mmcif.api.DictionaryApi import DictionaryApi

    containers: list[Any] = []
    for path in paths:
        with open_text(path) as fh:
            from mmcif.io.PdbxReader import PdbxReader

            PdbxReader(fh).read(containers)
    if not containers:
        raise RuntimeError(f"No dictionary containers parsed from: {paths}")
    return DictionaryApi(containerList=containers, consolidate=True)


def parse_manifest(path: Path) -> list[Entry]:
    rows: list[Entry] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        required = {"structure_id", "cif_path", "dictionary_paths"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Manifest missing columns: {sorted(missing)}")
        for row in reader:
            sid = (row.get("structure_id") or "").strip()
            if not sid or sid.startswith("#"):
                continue
            cif_path = Path((row.get("cif_path") or "").strip()).expanduser().resolve()
            dict_paths = tuple(
                Path(item.strip()).expanduser().resolve()
                for item in (row.get("dictionary_paths") or "").split(";")
                if item.strip()
            )
            kind = (row.get("kind") or "").strip()
            rows.append(Entry(sid, cif_path, dict_paths, kind))
    if not rows:
        raise ValueError(f"Manifest contains no benchmark rows: {path}")
    return rows


def write_manifest_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["structure_id", "cif_path", "dictionary_paths", "kind"])
        writer.writerow(["4HHB", "/absolute/path/4HHB.cif", "/absolute/path/mmcif_pdbx.dic", "PDB"])
        writer.writerow(["9A8K", "/absolute/path/9A8K.cif", "/absolute/path/mmcif_pdbx.dic;/absolute/path/mmcif_ihm_ext.dic", "IHM"])
        writer.writerow(["AF_AFA0A017SEY2F1", "/absolute/path/AF_AFA0A017SEY2F1.cif", "/absolute/path/mmcif_pdbx.dic;/absolute/path/mmcif_ma_ext.dic", "CSM"])


def validate_paths(entries: Sequence[Entry]) -> None:
    errors: list[str] = []
    for entry in entries:
        if not entry.cif_path.is_file():
            errors.append(f"Missing CIF: {entry.cif_path}")
        if not entry.dictionary_paths:
            errors.append(f"No dictionaries for {entry.structure_id}")
        for path in entry.dictionary_paths:
            if not path.is_file():
                errors.append(f"Missing dictionary for {entry.structure_id}: {path}")
    if errors:
        raise FileNotFoundError("\n".join(errors))


def set_cpu_affinity(cpu: int | None) -> str:
    if cpu is None:
        return "not requested"
    try:
        os.sched_setaffinity(0, {cpu})
        return f"pinned to CPU {cpu}"
    except Exception as exc:
        return f"failed to pin to CPU {cpu}: {exc}"


def canonical_decoded_digest(path: Path) -> tuple[str, dict[str, int]]:
    """Hash decoded content with type markers; used for candidate-vs-PR equality."""
    from mmcif.io.BinaryCifReader import BinaryCifReader

    containers = BinaryCifReader(storeStringsAsBytes=False).deserialize(str(path))
    h = hashlib.sha256()
    counts = {"containers": 0, "categories": 0, "columns": 0, "values": 0}
    for container in containers:
        counts["containers"] += 1
        h.update(b"CONTAINER\0")
        h.update(str(container.getName()).encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        for cat_name in container.getObjNameList():
            counts["categories"] += 1
            cat = container.getObj(cat_name)
            h.update(b"CATEGORY\0")
            h.update(str(cat_name).encode("utf-8", "surrogatepass"))
            h.update(b"\0")
            attrs = list(cat.getAttributeList())
            counts["columns"] += len(attrs)
            h.update(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))
            h.update(b"\0")
            for row_idx in range(cat.getRowCount()):
                for attr in attrs:
                    value = cat.getValue(attr, row_idx)
                    counts["values"] += 1
                    token = f"{type(value).__name__}:{repr(value)}\0"
                    h.update(token.encode("utf-8", "surrogatepass"))
    return h.hexdigest(), counts


def git_info(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.STDOUT).strip()
        except Exception as exc:
            return f"ERROR: {exc}"

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "status": run("status", "--short"),
        "remote": run("remote", "get-url", "origin"),
    }


# ---------------------------------------------------------------------------
# Exact encoder building blocks
# ---------------------------------------------------------------------------


def _private(self: Any, name: str) -> Any:
    return getattr(self, f"_BinaryCifEncoders__{name}")


def _typed_float_input(module: Any, self: Any, values: Sequence[Any]) -> Any:
    use_float64 = bool(_private(self, "useFloat64"))
    return module.TypedArray(list(values), "float_64" if use_float64 else "float_32")


def _finish_integer_chain(self: Any, transformed: Any, metadata: list[dict[str, Any]]) -> tuple[bytes, list[dict[str, Any]]]:
    packed, ip_meta = self.integerPackingEncoderTyped(transformed)
    if ip_meta is not None:
        metadata.append(ip_meta)
    encoded, ba_meta = self.byteArrayEncoderTyped(packed, "integer")
    metadata.append(ba_meta)
    return encoded.data, metadata


def _packing_byte_size(self: Any, typed_values: Any) -> int:
    packing = self._determine_packing(typed_values.data)
    return int(packing["size"]) * int(packing["bytes"])


def _build_exact_candidates(self: Any, scaled: Any, fp_meta: dict[str, Any]) -> list[tuple[int, Any, list[dict[str, Any]], str]]:
    """Build four candidates in the exact current order, reusing transforms."""
    candidates: list[tuple[int, Any, list[dict[str, Any]], str]] = []

    # 1. IntegerPacking
    candidates.append((_packing_byte_size(self, scaled), scaled, [fp_meta], "fp_ip_ba"))

    # 2. RunLength -> IntegerPacking
    rle, rle_meta = self.runLengthEncoderTyped(scaled)
    meta = [fp_meta] + ([rle_meta] if rle_meta is not None else [])
    candidates.append((_packing_byte_size(self, rle), rle, meta, "fp_rl_ip_ba"))

    # 3. Delta -> IntegerPacking
    delta, delta_meta = self.deltaEncoderTyped(scaled)
    meta = [fp_meta] + ([delta_meta] if delta_meta is not None else [])
    candidates.append((_packing_byte_size(self, delta), delta, meta, "fp_delta_ip_ba"))

    # 4. Delta -> RunLength -> IntegerPacking
    delta_rle, delta_rle_meta = self.runLengthEncoderTyped(delta)
    meta = [fp_meta]
    if delta_meta is not None:
        meta.append(delta_meta)
    if delta_rle_meta is not None:
        meta.append(delta_rle_meta)
    candidates.append((_packing_byte_size(self, delta_rle), delta_rle, meta, "fp_delta_rl_ip_ba"))
    return candidates


def make_exact_selector(module: Any, original_best: Callable[..., Any], short_only: bool = False) -> Callable[..., Any]:
    def selector(self: Any, col_data: Sequence[Any], factor: int):
        if len(col_data) == 0:
            return original_best(self, col_data, factor)
        if short_only and len(col_data) > 40:
            return original_best(self, col_data, factor)
        try:
            typed = _typed_float_input(module, self, col_data)
            scaled, fp_meta = self.fixedPointEncoderTyped(typed, factor)
            if len(col_data) <= 40:
                # Current Delta/RunLength methods both return the original input and
                # no metadata at <= 40, making all four outputs identical.
                return _finish_integer_chain(self, scaled, [fp_meta])
            candidates = _build_exact_candidates(self, scaled, fp_meta)
            # min() is stable: ties preserve current candidate order.
            _, transformed, metadata, _ = min(candidates, key=lambda item: item[0])
            return _finish_integer_chain(self, transformed, list(metadata))
        except Exception:
            return original_best(self, col_data, factor)

    return selector


def make_single_scale_specialized(module: Any, original_helper: Callable[..., Any]) -> Callable[..., Any]:
    def helper(self: Any, col_data: Sequence[Any], factor: int, encoder_list: Sequence[Any], item_name: str):
        try:
            typed = _typed_float_input(module, self, col_data)
            scaled, fp_meta = self.fixedPointEncoderTyped(typed, factor)
            transformed = scaled
            metadata: list[dict[str, Any]] = [fp_meta]
            for enc in encoder_list:
                name = enc[0] if isinstance(enc, tuple) else enc
                if name in {"FixedPoint", "IntegerPacking", "ByteArray"}:
                    continue
                if name == "Delta":
                    transformed, meta = self.deltaEncoderTyped(transformed)
                elif name == "RunLength":
                    transformed, meta = self.runLengthEncoderTyped(transformed)
                else:
                    return original_helper(self, col_data, factor, encoder_list, item_name)
                if meta is not None:
                    metadata.append(meta)
            return _finish_integer_chain(self, transformed, metadata)
        except Exception:
            return self.encode(col_data, ["ByteArray"], "float")

    return helper


def make_full_exact_float_pipeline(module: Any, original_float: Callable[..., Any]) -> Callable[..., Any]:
    def fixed_chain(self: Any, values: Sequence[Any], factor: int, transforms: Sequence[str]):
        typed = _typed_float_input(module, self, values)
        scaled, fp_meta = self.fixedPointEncoderTyped(typed, factor)
        transformed = scaled
        metadata: list[dict[str, Any]] = [fp_meta]
        for name in transforms:
            if name == "Delta":
                transformed, meta = self.deltaEncoderTyped(transformed)
            elif name == "RunLength":
                transformed, meta = self.runLengthEncoderTyped(transformed)
            else:
                raise ValueError(name)
            if meta is not None:
                metadata.append(meta)
        return _finish_integer_chain(self, transformed, metadata)

    def exact_general(self: Any, values: Sequence[Any], factor: int):
        typed = _typed_float_input(module, self, values)
        scaled, fp_meta = self.fixedPointEncoderTyped(typed, factor)
        if len(values) <= 40:
            return _finish_integer_chain(self, scaled, [fp_meta])
        candidates = _build_exact_candidates(self, scaled, fp_meta)
        _, transformed, metadata, _ = min(candidates, key=lambda item: item[0])
        return _finish_integer_chain(self, transformed, list(metadata))

    def float_pipeline(self: Any, col_data: Sequence[Any], col_mask: Sequence[int] | None, catName: str | None = None, atName: str | None = None):
        if col_mask:
            masked = [0.0 if m else d for m, d in zip(col_mask, col_data)]
        else:
            masked = col_data

        fallback = ["ByteArray"]
        if not self.USE_FIXED_POINT_FLOAT_ENCODING:
            return self.encode(masked, fallback, "float")

        get_item_name = _private(self, "getItemName")
        is_coord = _private(self, "isCoordinateItem")
        is_aniso = _private(self, "isAnisotropUItem")
        get_factor = _private(self, "getFloatFixedPointFactor")
        should_string = _private(self, "shouldUseStringFallbackForFloat")
        string_fallback = _private(self, "encodeFloatStringFallback")
        item_name = get_item_name(catName, atName)

        try:
            if self.USE_COORDINATE_CHAIN and is_coord(catName, atName):
                return fixed_chain(self, masked, self.COORDINATE_FIXED_POINT_FACTOR, ("Delta",))
            if self.USE_ANISOTROP_U_CHAIN and is_aniso(catName, atName):
                return fixed_chain(self, masked, self.ANISOTROP_U_FIXED_POINT_FACTOR, ("Delta",))
            if self.USE_OBJECT_RADIUS_CHAIN and item_name == self.OBJECT_RADIUS_ITEM:
                return fixed_chain(self, masked, self.OBJECT_RADIUS_FIXED_POINT_FACTOR, ())

            factor = get_factor(masked, catName=catName, atName=atName)
            if factor is None:
                if self.USE_STRING_FLOAT_FALLBACK and should_string(masked):
                    return string_fallback(col_data, col_mask)
                return self.encode(masked, fallback, "float")

            if self.USE_RUN_LENGTH_FLOAT_HINTS and item_name in self.RUN_LENGTH_FLOAT_ITEMS:
                return fixed_chain(self, masked, factor, ("RunLength",))
            return exact_general(self, masked, factor)
        except Exception:
            return self.encode(masked, fallback, "float")

    return float_pipeline


# ---------------------------------------------------------------------------
# Exact numeric hot-loop and writer-lifecycle alternatives
# ---------------------------------------------------------------------------


def make_fixedpoint_one_pass(module: Any) -> Callable[..., Any]:
    """Scale and validate in one pass while preserving current metadata/output."""

    def encoder(self: Any, typed: Any, factor: int):
        if typed.dtype and typed.dtype not in {"float_32", "float_64"}:
            raise TypeError(f"Only float arrays can be encoded with FixedPoint: {typed.dtype}")
        src_type = typed.dtype or ("float_64" if _private(self, "useFloat64") else "float_32")
        output: list[int] = []
        append = output.append
        floor = math.floor
        for raw in typed.data:
            value = int(floor(float(raw) * factor + 0.5))
            if value < INT32_MIN or value > INT32_MAX:
                raise TypeError("FixedPoint output does not fit in integer_32")
            append(value)
        to_bytes = _private(self, "toBytes")
        meta = {
            to_bytes("kind"): to_bytes("FixedPoint"),
            to_bytes("factor"): factor,
            to_bytes("srcType"): _private(self, "bCifTypeCodeD")[src_type],
        }
        return module.TypedArray(output, "integer_32"), meta

    return encoder


def make_delta_preallocated(module: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    """Avoid the temporary comprehension-plus-concatenation used by Delta."""

    def encoder(self: Any, typed: Any, minLen: int = 40):
        if typed.dtype and typed.dtype not in {"integer_8", "integer_16", "integer_32"}:
            return original(self, typed, minLen)
        values = typed.data
        n = len(values)
        if n <= minLen:
            return typed, None
        src_type = typed.dtype or "integer_32"
        to_bytes = _private(self, "toBytes")
        meta = {
            to_bytes("kind"): to_bytes("Delta"),
            to_bytes("origin"): values[0],
            to_bytes("srcType"): _private(self, "bCifTypeCodeD")[src_type],
        }
        output = [0] * n
        previous = values[0]
        for index in range(1, n):
            current = values[index]
            output[index] = current - previous
            previous = current
        return module.TypedArray(output, src_type), meta

    return encoder


def make_runlength_early_abort(module: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    """Stop building RLE output once it can no longer beat the input length."""

    def encoder(self: Any, typed: Any, minLen: int = 40):
        values = typed.data
        n = len(values)
        if n <= minLen:
            return typed, None
        if not values:
            return typed, None
        src_type = typed.dtype or "integer_32"
        output: list[int] = []
        extend = output.extend
        current = values[0]
        repeat = 1
        for value in values[1:]:
            if value == current:
                repeat += 1
                continue
            extend((current, repeat))
            if len(output) > n:
                return typed, None
            current = value
            repeat = 1
        extend((current, repeat))
        if len(output) > n:
            return typed, None
        to_bytes = _private(self, "toBytes")
        meta = {
            to_bytes("kind"): to_bytes("RunLength"),
            to_bytes("srcType"): _private(self, "bCifTypeCodeD")[src_type],
            to_bytes("srcSize"): n,
        }
        return module.TypedArray(output, "integer_32"), meta

    return encoder


def make_reused_writer_encoder(module: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    """Reuse the stateless encoder object instead of constructing one per column."""

    def encode_column(self: Any, col_data: Sequence[Any], data_type: str, catName: str | None = None, atName: str | None = None):
        try:
            encoder = getattr(self, "_bcif_speed_cached_encoder", None)
            if encoder is None:
                encoder = module.BinaryCifEncoders(
                    defaultStringEncoding=getattr(self, "_BinaryCifWriter__defaultStringEncoding"),
                    storeStringsAsBytes=getattr(self, "_BinaryCifWriter__storeStringsAsBytes"),
                    useFloat64=getattr(self, "_BinaryCifWriter__useFloat64"),
                )
                setattr(self, "_bcif_speed_cached_encoder", encoder)

            mask_dict = None
            type_encoder = {
                "string": "StringArrayMasked",
                "integer": "IntArrayMasked",
                "float": "FloatArrayMasked",
            }[data_type]
            mask = encoder.getMask(col_data)
            encoded, metadata = encoder.encodeWithMask(
                col_data,
                mask,
                type_encoder,
                catName=catName,
                atName=atName,
            )
            if mask:
                mask_typed = module.TypedArray(mask, "unsigned_integer_8")
                mask_encoded, mask_metadata = encoder.encode(mask_typed, ["RunLength", "ByteArray"], "integer")
                to_bytes = getattr(self, "_BinaryCifWriter__toBytes")
                mask_dict = {
                    to_bytes("data"): mask_encoded.data,
                    to_bytes("encoding"): mask_metadata,
                }
            return mask_dict, encoded, metadata
        except Exception:
            # Any signature or implementation drift falls back to the branch code.
            return original(self, col_data, data_type, catName, atName)

    return encode_column


# ---------------------------------------------------------------------------
# ByteArray and IntegerPacking exact alternatives
# ---------------------------------------------------------------------------


def _bytearray_type_info(module: Any, self: Any, typed: Any, data_type: str) -> tuple[int, str, str]:
    code_map = _private(self, "bCifTypeCodeD")
    if data_type == "float":
        type_name = "float_64" if _private(self, "useFloat64") else "float_32"
        code = code_map[type_name]
    elif typed.dtype:
        type_name = typed.dtype
        code = code_map[type_name]
    else:
        code = _private(self, "getIntegerPackingType")(typed.data)
        type_name = module.BinaryCifDecoders.bCifCodeTypeD[code]
    fmt = module.BinaryCifDecoders.bCifTypeD[type_name]["struct_format_code"]
    return code, type_name, fmt


def make_bytearray_repeat(module: Any) -> Callable[..., Any]:
    def encoder(self: Any, typed: Any, data_type: str):
        code, _, fmt = _bytearray_type_info(module, self, typed, data_type)
        to_bytes = _private(self, "toBytes")
        meta = {to_bytes("kind"): to_bytes("ByteArray"), to_bytes("type"): code}
        values = typed.data
        packed = struct.pack(f"<{len(values)}{fmt}", *values) if values else b""
        return module.TypedArray(packed), meta

    return encoder


def make_bytearray_array(module: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    typecodes = {
        "integer_8": "b",
        "unsigned_integer_8": "B",
        "integer_16": "h",
        "unsigned_integer_16": "H",
        "integer_32": "i",
        "unsigned_integer_32": "I",
        "float_32": "f",
        "float_64": "d",
    }
    expected_sizes = {
        "integer_8": 1,
        "unsigned_integer_8": 1,
        "integer_16": 2,
        "unsigned_integer_16": 2,
        "integer_32": 4,
        "unsigned_integer_32": 4,
        "float_32": 4,
        "float_64": 8,
    }

    def encoder(self: Any, typed: Any, data_type: str):
        code, type_name, _ = _bytearray_type_info(module, self, typed, data_type)
        typecode = typecodes.get(type_name)
        if typecode is None:
            return original(self, typed, data_type)
        try:
            arr = array.array(typecode, typed.data)
            if arr.itemsize != expected_sizes[type_name]:
                return original(self, typed, data_type)
            if sys.byteorder != "little" and arr.itemsize > 1:
                arr.byteswap()
            encoded = arr.tobytes()
            to_bytes = _private(self, "toBytes")
            meta = {to_bytes("kind"): to_bytes("ByteArray"), to_bytes("type"): code}
            return module.TypedArray(encoded), meta
        except Exception:
            return original(self, typed, data_type)

    return encoder


def make_fast_determine_packing(original: Callable[..., Any]) -> Callable[..., Any]:
    def determine(self: Any, values: Sequence[int]):
        if not values:
            return original(self, values)
        try:
            min_v = min(values)
            signed = min_v < 0
            size8 = len(values)
            size16 = len(values)
            if signed:
                lower8, lower16 = -128, -32768
                for value in values:
                    if value >= 0:
                        size8 += value // 127
                        size16 += value // 32767
                    else:
                        size8 += value // lower8
                        size16 += value // lower16
            else:
                for value in values:
                    size8 += value // 255
                    size16 += value // 65535
            dlen = len(values)
            if dlen * 4 < size16 * 2:
                return {"size": dlen, "bytes": 4, "isSigned": signed}
            if size16 * 2 < size8:
                return {"size": size16, "bytes": 2, "isSigned": signed}
            return {"size": size8, "bytes": 1, "isSigned": signed}
        except Exception:
            return original(self, values)

    return determine


def make_fast_integer_pack(module: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    def encoder(self: Any, typed: Any):
        if typed.dtype and typed.dtype != "integer_32":
            return original(self, typed)
        try:
            packing = self._determine_packing(typed.data)
            nbytes = int(packing["bytes"])
            signed = bool(packing["isSigned"])
            if nbytes == 4:
                return typed, None

            upper = (0x7F if nbytes == 1 else 0x7FFF) if signed else (0xFF if nbytes == 1 else 0xFFFF)
            lower = -upper - 1
            out = [0] * int(packing["size"])
            index = 0
            for original_value in typed.data:
                value = int(original_value)
                if value >= 0:
                    q, remainder = divmod(value, upper)
                    if q:
                        out[index:index + q] = itertools.repeat(upper, q)
                        index += q
                else:
                    q = value // lower
                    remainder = value - q * lower
                    if q:
                        out[index:index + q] = itertools.repeat(lower, q)
                        index += q
                out[index] = remainder
                index += 1
            if index != len(out):
                return original(self, typed)

            dtype = ("integer_8" if signed else "unsigned_integer_8") if nbytes == 1 else ("integer_16" if signed else "unsigned_integer_16")
            to_bytes = _private(self, "toBytes")
            meta = {
                to_bytes("kind"): to_bytes("IntegerPacking"),
                to_bytes("byteCount"): nbytes,
                to_bytes("srcSize"): len(typed.data),
                to_bytes("isUnsigned"): not signed,
            }
            return module.TypedArray(out, dtype), meta
        except Exception:
            return original(self, typed)

    return encoder


# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------


def candidate_selector(chains: Sequence[Sequence[Any]], short_first: bool = False) -> Callable[..., Any]:
    def selector(self: Any, col_data: Sequence[Any], factor: int):
        if short_first and len(col_data) <= 40:
            local = [[("FixedPoint", factor), "IntegerPacking", "ByteArray"]]
        else:
            local = []
            for chain in chains:
                local.append([(("FixedPoint", factor) if step == "FixedPoint" else step) for step in chain])
        best = None
        for encoders in local:
            try:
                encoded, metadata = self.encode(list(col_data), encoders, "float")
                payload = encoded.data if hasattr(encoded, "data") else encoded
                size = len(payload)
                if best is None or size < best[0]:
                    best = (size, encoded, metadata)
            except Exception:
                continue
        return (None, None) if best is None else (best[1], best[2])

    return selector


def build_variants(module: Any, originals: Mapping[str, Any]) -> dict[str, Variant]:
    Enc = module.BinaryCifEncoders
    Writer = module.BinaryCifWriter
    best_name = "_BinaryCifEncoders__encodeBestFixedPointChain"
    helper_name = "_BinaryCifEncoders__encodeFixedPointChainOrFallback"
    writer_column_name = "_BinaryCifWriter__encodeColumnData"
    original_best = originals[best_name]
    original_helper = originals[helper_name]
    original_float = originals["floatArrayMaskedEncoder"]
    original_fixed = originals["fixedPointEncoderTyped"]
    original_delta = originals["deltaEncoderTyped"]
    original_rle = originals["runLengthEncoderTyped"]
    original_byte = originals["byteArrayEncoderTyped"]
    original_determine = originals["_determine_packing"]
    original_pack = originals["integerPackingEncoderTyped"]
    original_writer_column = originals[writer_column_name]

    def no_op(_module: Any, _orig: Mapping[str, Any]) -> None:
        return None

    def apply_short(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, best_name, make_exact_selector(module, original_best, short_only=True))

    def apply_cached(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, best_name, make_exact_selector(module, original_best, short_only=False))

    def apply_specialized(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, helper_name, make_single_scale_specialized(module, original_helper))

    def apply_float_pipeline(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "floatArrayMaskedEncoder", make_full_exact_float_pipeline(module, original_float))

    def apply_float_bytearray_reference(_module: Any, _orig: Mapping[str, Any]) -> None:
        def bytearray_float(self: Any, col_data: Sequence[Any], col_mask: Sequence[int] | None, catName: str | None = None, atName: str | None = None):
            masked = [0.0 if m else d for m, d in zip(col_mask, col_data)] if col_mask else col_data
            return self.encode(masked, ["ByteArray"], "float")
        setattr(Enc, "floatArrayMaskedEncoder", bytearray_float)

    def apply_fixed(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "fixedPointEncoderTyped", make_fixedpoint_one_pass(module))

    def apply_delta(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "deltaEncoderTyped", make_delta_preallocated(module, original_delta))

    def apply_rle(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "runLengthEncoderTyped", make_runlength_early_abort(module, original_rle))

    def apply_hotloops(_module: Any, _orig: Mapping[str, Any]) -> None:
        apply_fixed(_module, _orig)
        apply_delta(_module, _orig)
        apply_rle(_module, _orig)

    def apply_repeat(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "byteArrayEncoderTyped", make_bytearray_repeat(module))

    def apply_array(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "byteArrayEncoderTyped", make_bytearray_array(module, original_byte))

    def apply_pack(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "_determine_packing", make_fast_determine_packing(original_determine))
        setattr(Enc, "integerPackingEncoderTyped", make_fast_integer_pack(module, original_pack))

    def apply_reuse(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Writer, writer_column_name, make_reused_writer_encoder(module, original_writer_column))

    def apply_all_common(_module: Any, _orig: Mapping[str, Any]) -> None:
        setattr(Enc, "floatArrayMaskedEncoder", make_full_exact_float_pipeline(module, original_float))
        apply_hotloops(_module, _orig)
        apply_pack(_module, _orig)
        apply_reuse(_module, _orig)

    def apply_all_exact_array(_module: Any, _orig: Mapping[str, Any]) -> None:
        apply_all_common(_module, _orig)
        apply_array(_module, _orig)

    def apply_all_exact_repeat(_module: Any, _orig: Mapping[str, Any]) -> None:
        apply_all_common(_module, _orig)
        apply_repeat(_module, _orig)

    chains = {
        "best2_empirical": [
            ("FixedPoint", "RunLength", "IntegerPacking", "ByteArray"),
            ("FixedPoint", "Delta", "RunLength", "IntegerPacking", "ByteArray"),
        ],
        "single_ip": [("FixedPoint", "IntegerPacking", "ByteArray")],
        "single_rle": [("FixedPoint", "RunLength", "IntegerPacking", "ByteArray")],
        "single_delta": [("FixedPoint", "Delta", "IntegerPacking", "ByteArray")],
        "single_delta_rle": [("FixedPoint", "Delta", "RunLength", "IntegerPacking", "ByteArray")],
    }

    variants = {
        "pr_current": Variant("pr_current", "Unmodified current PR: fully encode all four general FixedPoint candidates.", True, True, False, no_op),
        "float_bytearray_reference": Variant("float_bytearray_reference", "Reference path that sends every float column directly to ByteArray, approximating the pre-PR float behavior.", False, True, True, apply_float_bytearray_reference),
        "short_exact": Variant("short_exact", "Guaranteed shortcut: <=40 values use FP->IP->BA; larger columns retain current best-of-four.", True, True, False, apply_short),
        "selector_cached_exact": Variant("selector_cached_exact", "Cache FixedPoint/Delta/RLE transforms, predict exact packed byte lengths, encode only the winning general candidate.", True, True, False, apply_cached),
        "specialized_single_scale_exact": Variant("specialized_single_scale_exact", "Avoid duplicate FixedPoint scaling in specialized coordinate/anisotrop/radius/hint chains.", True, True, False, apply_specialized),
        "float_pipeline_exact": Variant("float_pipeline_exact", "Single-scale specialized paths plus exact cached selector for general floats.", True, True, False, apply_float_pipeline),
        "fixedpoint_one_pass_exact": Variant("fixedpoint_one_pass_exact", "Fuse FixedPoint scaling and signed-Int32 validation into one pass.", True, True, False, apply_fixed),
        "delta_prealloc_exact": Variant("delta_prealloc_exact", "Preallocate Delta output instead of creating and concatenating two lists.", True, True, False, apply_delta),
        "runlength_early_abort_exact": Variant("runlength_early_abort_exact", "Stop RLE construction as soon as expansion beyond the source length is irreversible.", True, True, False, apply_rle),
        "numeric_hotloops_exact": Variant("numeric_hotloops_exact", "Combine one-pass FixedPoint, preallocated Delta, and early-abort RunLength.", True, True, False, apply_hotloops),
        "bytearray_repeat_exact": Variant("bytearray_repeat_exact", "Use a compact struct repeat count instead of constructing a format character per value.", True, True, False, apply_repeat),
        "bytearray_array_exact": Variant("bytearray_array_exact", "Use array.array with explicit little-endian conversion; falls back to struct when unsafe.", True, True, False, apply_array),
        "fast_integer_pack_exact": Variant("fast_integer_pack_exact", "Use integer arithmetic, fewer scans, and direct quotient/remainder packing.", True, True, False, apply_pack),
        "reuse_encoder_exact": Variant("reuse_encoder_exact", "Reuse one BinaryCifEncoders instance across all columns serialized by a writer.", True, True, False, apply_reuse),
        "all_exact": Variant("all_exact", "Combine all exact optimizations with array.array ByteArray output.", True, True, False, apply_all_exact_array),
        "all_exact_repeat": Variant("all_exact_repeat", "Combine all exact optimizations with compact repeated-struct ByteArray output.", True, True, False, apply_all_exact_repeat),
    }

    for name, chain_list in chains.items():
        short_first = name == "best2_empirical"

        def make_apply(chain_list=chain_list, short_first=short_first):
            def apply(_module: Any, _orig: Mapping[str, Any]) -> None:
                setattr(Enc, best_name, candidate_selector(chain_list, short_first=short_first))
            return apply

        descriptions = {
            "best2_empirical": "<=40 shortcut; for larger general columns compare only RLE and Delta+RLE candidates.",
            "single_ip": "Hardcode FP->IP->BA for general floats.",
            "single_rle": "Hardcode FP->RLE->IP->BA for general floats.",
            "single_delta": "Hardcode FP->Delta->IP->BA for general floats.",
            "single_delta_rle": "Hardcode FP->Delta->RLE->IP->BA for general floats.",
        }
        variants[name] = Variant(name, descriptions[name], False, True, True, make_apply())

    return variants


# ---------------------------------------------------------------------------
# Benchmark execution
# ---------------------------------------------------------------------------


def variant_order(names: Sequence[str], repetition: int, rng: random.Random) -> list[str]:
    names = list(names)
    if repetition == 0:
        return names
    # Balanced rotation plus reversal, then a deterministic shuffle every third pass.
    shift = repetition % len(names)
    ordered = names[shift:] + names[:shift]
    if repetition % 2:
        ordered.reverse()
    if repetition % 3 == 2:
        rng.shuffle(ordered)
    return ordered


def serialize_once(module: Any, dapi: Any, containers: list[Any], output: Path, args: argparse.Namespace) -> tuple[float, float, bool, str]:
    writer = module.BinaryCifWriter(
        dapi,
        storeStringsAsBytes=args.store_strings_as_bytes,
        applyTypes=True,
        useStringTypes=False,
        useFloat64=args.use_float64,
        copyInputData=False,
        ignoreCastErrors=False,
        applyMolStarTypes=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    wall0 = time.perf_counter_ns()
    cpu0 = time.process_time_ns()
    try:
        ok = bool(writer.serialize(str(output), containers))
        error = "" if ok else "BinaryCifWriter.serialize() returned False"
    except Exception:
        ok = False
        error = traceback.format_exc()
    cpu1 = time.process_time_ns()
    wall1 = time.perf_counter_ns()
    return (wall1 - wall0) / 1e9, (cpu1 - cpu0) / 1e9, ok, error


def benchmark(
    module: Any,
    patcher: PatchManager,
    variants: Mapping[str, Variant],
    selected: Sequence[str],
    entries: Sequence[Entry],
    dictionaries: Mapping[tuple[Path, ...], Any],
    timing_root: Path,
    saved_root: Path,
    clone_cache_root: Path,
    args: argparse.Namespace,
) -> list[RunResult]:
    results: list[RunResult] = []
    rng = random.Random(args.seed)

    for entry_index, entry in enumerate(entries, 1):
        print(f"\n[{entry_index}/{len(entries)}] {entry.structure_id} ({entry.kind or 'unclassified'})")
        print(f"  preparing one-structure disk clone cache: {entry.cif_path}", flush=True)
        provider = ContainerProvider(
            entry.cif_path,
            read_cif,
            clone_cache_root / f"{safe_name(entry.structure_id)}.pickle",
        )
        print(f"  prepared {entry.structure_id}", flush=True)
        dapi = dictionaries[entry.dictionary_paths]

        # Warmups are intentionally not included in the summary.
        for warmup in range(args.warmups):
            order = variant_order(selected, warmup, rng)
            for order_index, name in enumerate(order):
                patcher.reset()
                variants[name].apply(module, patcher.originals)
                containers = provider.clone()
                output = timing_root / safe_name(name) / f"{safe_name(entry.structure_id)}.warmup.bcif"
                wall, cpu, ok, error = serialize_once(module, dapi, containers, output, args)
                if not ok:
                    raise RuntimeError(f"Warmup failed for {entry.structure_id}/{name}: {error}")
                print(f"  warmup {warmup + 1}: {name:34s} {wall:10.6f} s", flush=True)
                # Warmup files are not evidence artifacts; remove immediately so a
                # full corpus cannot fill /dev/shm or the timing filesystem.
                output.unlink(missing_ok=True)
                del containers

        for repetition in range(args.runs):
            order = variant_order(selected, repetition, rng)
            for order_index, name in enumerate(order):
                patcher.reset()
                variants[name].apply(module, patcher.originals)
                containers = provider.clone()
                output = timing_root / safe_name(name) / f"{safe_name(entry.structure_id)}.run{repetition + 1}.bcif"
                if output.exists():
                    output.unlink()
                if args.disable_gc:
                    gc.collect()
                    gc.disable()
                try:
                    wall, cpu, ok, error = serialize_once(module, dapi, containers, output, args)
                finally:
                    if args.disable_gc:
                        gc.enable()
                size = output.stat().st_size if ok and output.is_file() else -1
                digest = sha256_file(output) if ok and output.is_file() else ""
                result = RunResult(entry.structure_id, entry.kind, name, "measured", repetition + 1, order_index, wall, cpu, size, digest, ok, error)
                results.append(result)
                print(f"  run {repetition + 1:02d}: {name:34s} {wall:10.6f} s  {size:12,d} bytes", flush=True)
                if not ok:
                    raise RuntimeError(f"Measured run failed for {entry.structure_id}/{name}: {error}")
                # Size and SHA-256 have already been recorded. Remove each timed
                # output immediately instead of retaining hundreds of BCIF files.
                output.unlink(missing_ok=True)
                del containers

        # Save one stable output per strategy for hashing, decoding, and Java verification.
        for name in selected:
            patcher.reset()
            variants[name].apply(module, patcher.originals)
            containers = provider.clone()
            output = saved_root / safe_name(name) / f"{safe_name(entry.structure_id)}.bcif"
            _, _, ok, error = serialize_once(module, dapi, containers, output, args)
            if not ok:
                raise RuntimeError(f"Saved output failed for {entry.structure_id}/{name}: {error}")
            del containers

        provider.cleanup()
        del provider
        gc.collect()

    patcher.reset()
    return results


def validate_outputs(
    variants: Mapping[str, Variant],
    selected: Sequence[str],
    entries: Sequence[Entry],
    saved_root: Path,
    java_verifier: Path | None,
    results_dir: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    digest_cache: dict[Path, tuple[str, dict[str, int]]] = {}

    for entry in entries:
        base_path = saved_root / "pr_current" / f"{safe_name(entry.structure_id)}.bcif"
        base_sha = sha256_file(base_path)
        base_decoded, base_counts = canonical_decoded_digest(base_path)
        digest_cache[base_path] = (base_decoded, base_counts)
        for name in selected:
            path = saved_root / safe_name(name) / f"{safe_name(entry.structure_id)}.bcif"
            sha = sha256_file(path)
            decoded, counts = canonical_decoded_digest(path)
            digest_cache[path] = (decoded, counts)
            variant = variants[name]
            rows.append(
                {
                    "structure_id": entry.structure_id,
                    "kind": entry.kind,
                    "variant": name,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha,
                    "byte_identical_to_pr": sha == base_sha,
                    "semantic_digest": decoded,
                    "semantic_identical_to_pr": decoded == base_decoded,
                    "expected_byte_identical": variant.expected_byte_identical,
                    "expected_semantic_identical": variant.expected_semantic_identical,
                    "decoded_values": counts["values"],
                    "java_verifier": "not_requested" if java_verifier is None else "pending",
                }
            )

    if java_verifier is not None:
        java_logs = results_dir / "java_verifier"
        java_logs.mkdir(parents=True, exist_ok=True)
        status: dict[str, str] = {}
        for name in selected:
            directory = saved_root / safe_name(name)
            log_path = java_logs / f"{safe_name(name)}.log"
            proc = subprocess.run(
                ["java", "-jar", str(java_verifier), str(directory)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
            status[name] = "PASS" if proc.returncode == 0 else f"FAIL({proc.returncode})"
        for row in rows:
            row["java_verifier"] = status[row["variant"]]

    return rows


def run_memory_diagnostics(
    module: Any,
    patcher: PatchManager,
    variants: Mapping[str, Variant],
    selected: Sequence[str],
    entries: Sequence[Entry],
    providers: Mapping[str, ContainerProvider],
    dictionaries: Mapping[tuple[Path, ...], Any],
    results_dir: Path,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    if not args.measure_memory:
        return []
    rows: list[dict[str, Any]] = []
    root = results_dir / "memory_outputs"
    for entry in entries:
        for name in selected:
            patcher.reset()
            variants[name].apply(module, patcher.originals)
            containers = providers[entry.structure_id].clone()
            output = root / name / f"{safe_name(entry.structure_id)}.bcif"
            gc.collect()
            tracemalloc.start()
            wall, cpu, ok, error = serialize_once(module, dictionaries[entry.dictionary_paths], containers, output, args)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            rows.append({
                "structure_id": entry.structure_id,
                "variant": name,
                "wall_seconds_instrumented": wall,
                "cpu_seconds_instrumented": cpu,
                "peak_tracemalloc_bytes": peak,
                "success": ok,
                "error": error,
            })
    patcher.reset()
    return rows


def run_profiles(
    module: Any,
    patcher: PatchManager,
    variants: Mapping[str, Variant],
    selected: Sequence[str],
    entries: Sequence[Entry],
    providers: Mapping[str, ContainerProvider],
    dictionaries: Mapping[tuple[Path, ...], Any],
    results_dir: Path,
    args: argparse.Namespace,
) -> None:
    if not args.profile_structure:
        return
    profile_sids = set(args.profile_structure)
    profile_variants = set(args.profile_variant or ["pr_current", "all_exact"])
    profile_dir = results_dir / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry.structure_id not in profile_sids:
            continue
        for name in selected:
            if name not in profile_variants:
                continue
            patcher.reset()
            variants[name].apply(module, patcher.originals)
            containers = providers[entry.structure_id].clone()
            output = profile_dir / f"{safe_name(entry.structure_id)}.{safe_name(name)}.bcif"
            profiler = cProfile.Profile()
            profiler.enable()
            _, _, ok, error = serialize_once(module, dictionaries[entry.dictionary_paths], containers, output, args)
            profiler.disable()
            if not ok:
                raise RuntimeError(error)
            prof_path = profile_dir / f"{safe_name(entry.structure_id)}.{safe_name(name)}.prof"
            txt_path = profile_dir / f"{safe_name(entry.structure_id)}.{safe_name(name)}.txt"
            profiler.dump_stats(str(prof_path))
            import pstats

            stream = io.StringIO()
            stats = pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative")
            stats.print_stats(100)
            txt_path.write_text(stream.getvalue(), encoding="utf-8")
    patcher.reset()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_tsv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(fieldnames or rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summarize_runs(runs: Sequence[RunResult], validations: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    validation_index = {(r["structure_id"], r["variant"]): r for r in validations}
    grouped: MutableMapping[tuple[str, str], list[RunResult]] = {}
    for run in runs:
        grouped.setdefault((run.structure_id, run.variant), []).append(run)

    summary: list[dict[str, Any]] = []
    for (sid, variant), group in grouped.items():
        walls = [r.wall_seconds for r in group]
        cpus = [r.cpu_seconds for r in group]
        sizes = {r.size_bytes for r in group}
        shas = {r.sha256 for r in group}
        if len(sizes) != 1 or len(shas) != 1:
            raise RuntimeError(f"Non-deterministic output across runs: {sid}/{variant}")
        validation = validation_index[(sid, variant)]
        summary.append(
            {
                "structure_id": sid,
                "kind": group[0].kind,
                "variant": variant,
                "runs": len(group),
                "median_wall_seconds": statistics.median(walls),
                "mad_wall_seconds": median_abs_deviation(walls),
                "p10_wall_seconds": percentile(walls, 0.10),
                "p90_wall_seconds": percentile(walls, 0.90),
                "min_wall_seconds": min(walls),
                "max_wall_seconds": max(walls),
                "median_cpu_seconds": statistics.median(cpus),
                "size_bytes": next(iter(sizes)),
                "sha256": next(iter(shas)),
                "byte_identical_to_pr": validation["byte_identical_to_pr"],
                "semantic_identical_to_pr": validation["semantic_identical_to_pr"],
                "java_verifier": validation["java_verifier"],
            }
        )

    base = {row["structure_id"]: row for row in summary if row["variant"] == "pr_current"}
    for row in summary:
        b = base[row["structure_id"]]
        row["wall_change_percent_vs_pr"] = pct_change(row["median_wall_seconds"], b["median_wall_seconds"])
        row["wall_faster_percent_vs_pr"] = pct_faster(row["median_wall_seconds"], b["median_wall_seconds"])
        row["size_change_bytes_vs_pr"] = row["size_bytes"] - b["size_bytes"]
        row["size_change_percent_vs_pr"] = pct_change(row["size_bytes"], b["size_bytes"])

    by_variant: MutableMapping[str, list[dict[str, Any]]] = {}
    for row in summary:
        by_variant.setdefault(row["variant"], []).append(row)

    aggregate: list[dict[str, Any]] = []
    for variant, rows in by_variant.items():
        total_time = sum(float(r["median_wall_seconds"]) for r in rows)
        total_cpu = sum(float(r["median_cpu_seconds"]) for r in rows)
        total_size = sum(int(r["size_bytes"]) for r in rows)
        aggregate.append(
            {
                "variant": variant,
                "structures": len(rows),
                "sum_median_wall_seconds": total_time,
                "sum_median_cpu_seconds": total_cpu,
                "total_size_bytes": total_size,
                "all_byte_identical_to_pr": all(bool(r["byte_identical_to_pr"]) for r in rows),
                "all_semantic_identical_to_pr": all(bool(r["semantic_identical_to_pr"]) for r in rows),
                "all_java_verifier_pass": all(r["java_verifier"] in {"PASS", "not_requested"} for r in rows),
                "median_per_structure_wall_change_percent_vs_pr": statistics.median(float(r["wall_change_percent_vs_pr"]) for r in rows),
                "median_per_structure_size_change_percent_vs_pr": statistics.median(float(r["size_change_percent_vs_pr"]) for r in rows),
            }
        )
    base_agg = next(row for row in aggregate if row["variant"] == "pr_current")
    for row in aggregate:
        row["aggregate_wall_faster_percent_vs_pr"] = pct_faster(row["sum_median_wall_seconds"], base_agg["sum_median_wall_seconds"])
        row["aggregate_wall_change_percent_vs_pr"] = pct_change(row["sum_median_wall_seconds"], base_agg["sum_median_wall_seconds"])
        row["aggregate_size_change_bytes_vs_pr"] = row["total_size_bytes"] - base_agg["total_size_bytes"]
        row["aggregate_size_change_percent_vs_pr"] = pct_change(row["total_size_bytes"], base_agg["total_size_bytes"])
    return summary, aggregate


def pareto_frontier(aggregate: Sequence[Mapping[str, Any]]) -> list[str]:
    frontier: list[str] = []
    for row in aggregate:
        dominated = False
        for other in aggregate:
            if other is row:
                continue
            no_slower = float(other["sum_median_wall_seconds"]) <= float(row["sum_median_wall_seconds"])
            no_larger = int(other["total_size_bytes"]) <= int(row["total_size_bytes"])
            strictly_better = (
                float(other["sum_median_wall_seconds"]) < float(row["sum_median_wall_seconds"])
                or int(other["total_size_bytes"]) < int(row["total_size_bytes"])
            )
            if no_slower and no_larger and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(str(row["variant"]))
    return sorted(frontier)


def write_report(
    path: Path,
    variants: Mapping[str, Variant],
    selected: Sequence[str],
    aggregate: Sequence[Mapping[str, Any]],
    summary: Sequence[Mapping[str, Any]],
    validations: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> None:
    agg_by_name = {str(row["variant"]): row for row in aggregate}
    exact_candidates = [
        agg_by_name[name]
        for name in selected
        if variants[name].expected_byte_identical
        and bool(agg_by_name[name]["all_byte_identical_to_pr"])
        and bool(agg_by_name[name]["all_semantic_identical_to_pr"])
        and bool(agg_by_name[name]["all_java_verifier_pass"])
    ]
    exact_candidates.sort(key=lambda row: float(row["sum_median_wall_seconds"]))
    tradeoffs = [agg_by_name[name] for name in selected if variants[name].tradeoff]
    tradeoffs.sort(key=lambda row: float(row["sum_median_wall_seconds"]))

    lines: list[str] = []
    lines.append("# BinaryCIF speed-optimization benchmark report")
    lines.append("")
    lines.append(f"Generated: {metadata['generated_utc']}")
    lines.append(f"Script version: `{SCRIPT_VERSION}`")
    lines.append(f"Repository commit: `{metadata['git']['commit']}`")
    lines.append("")
    lines.append("## Decision rule")
    lines.append("")
    lines.append("A method is considered safe only when every tested file is byte-identical to the current PR output, decodes to the same typed values, and passes the optional Java verifier. Tradeoff methods are ranked separately.")
    lines.append("")
    lines.append("## Aggregate results")
    lines.append("")
    lines.append("| Variant | Sum of medians (s) | Faster vs PR | Total bytes | Size change vs PR | Byte-identical | Semantic-identical | Java |")
    lines.append("|---|---:|---:|---:|---:|:---:|:---:|:---:|")
    for row in sorted(aggregate, key=lambda r: float(r["sum_median_wall_seconds"])):
        lines.append(
            f"| `{row['variant']}` | {float(row['sum_median_wall_seconds']):.6f} | "
            f"{float(row['aggregate_wall_faster_percent_vs_pr']):+.3f}% | {int(row['total_size_bytes']):,} | "
            f"{float(row['aggregate_size_change_percent_vs_pr']):+.6f}% | "
            f"{'yes' if row['all_byte_identical_to_pr'] else 'no'} | "
            f"{'yes' if row['all_semantic_identical_to_pr'] else 'no'} | "
            f"{'pass' if row['all_java_verifier_pass'] else 'fail'} |"
        )

    lines.append("")
    lines.append("## Best verified no-regression method")
    lines.append("")
    if exact_candidates:
        best = exact_candidates[0]
        lines.append(
            f"`{best['variant']}` is the fastest strategy that met every configured no-regression check. "
            f"It was {float(best['aggregate_wall_faster_percent_vs_pr']):.3f}% faster than the current PR across the aggregate corpus, with {float(best['aggregate_size_change_percent_vs_pr']):.6f}% aggregate size change."
        )
    else:
        lines.append("No candidate satisfied all configured no-regression checks. Do not ship an optimization from this run.")

    lines.append("")
    lines.append("## Tradeoff methods")
    lines.append("")
    if tradeoffs:
        for row in tradeoffs:
            lines.append(
                f"- `{row['variant']}`: {float(row['aggregate_wall_faster_percent_vs_pr']):+.3f}% faster; "
                f"{float(row['aggregate_size_change_percent_vs_pr']):+.6f}% size change; "
                f"semantic equality={'yes' if row['all_semantic_identical_to_pr'] else 'no'}."
            )
    else:
        lines.append("No tradeoff methods were selected.")

    lines.append("")
    lines.append("## Pareto frontier")
    lines.append("")
    lines.append(", ".join(f"`{name}`" for name in pareto_frontier(aggregate)))
    lines.append("")
    lines.append("## Variant definitions")
    lines.append("")
    for name in selected:
        variant = variants[name]
        guarantee = "expected byte-identical" if variant.expected_byte_identical else "empirical/tradeoff"
        lines.append(f"- `{name}` ({guarantee}): {variant.description}")

    lines.append("")
    lines.append("## Evidence files")
    lines.append("")
    lines.extend(
        [
            "- `runs.tsv`: every measured repetition.",
            "- `summary.tsv`: per-structure medians, variability, size, and validation.",
            "- `aggregate.tsv`: corpus-level totals and comparisons.",
            "- `validation.tsv`: SHA-256, decoded digest, and Java-verifier result.",
            "- `variants.tsv`: strategy definitions and expected guarantees.",
            "- `metadata.json`: environment, source hash, Git state, and arguments.",
            "- `profiles/`: optional cProfile output.",
            "- `memory.tsv`: optional tracemalloc measurements.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_metadata(args: argparse.Namespace, repo: Path, source_path: Path, affinity: str, entries: Sequence[Entry], selected: Sequence[str]) -> dict[str, Any]:
    return {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "script_version": SCRIPT_VERSION,
        "argv": sys.argv,
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity": affinity,
        "repo": str(repo),
        "git": git_info(repo),
        "writer_source": str(source_path),
        "writer_source_sha256": sha256_file(source_path),
        "variants": list(selected),
        "entries": [
            {
                "structure_id": e.structure_id,
                "kind": e.kind,
                "cif_path": str(e.cif_path),
                "cif_sha256": sha256_file(e.cif_path),
                "dictionary_paths": [str(p) for p in e.dictionary_paths],
                "dictionary_sha256": {str(p): sha256_file(p) for p in e.dictionary_paths},
            }
            for e in entries
        ],
        "settings": vars(args),
    }


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Local py-mmcif repository root on the PR branch.")
    parser.add_argument("--manifest", type=Path, help="TSV with structure_id, cif_path, dictionary_paths, and optional kind.")
    parser.add_argument("--write-manifest-template", type=Path, help="Write a manifest template and exit.")
    parser.add_argument("--results-dir", type=Path, default=Path("benchmarks/results/bcif_speed_optimizations"))
    parser.add_argument("--timing-root", type=Path, help="Temporary output root. Defaults to /dev/shm when available.")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--seed", type=int, default=20260624)
    parser.add_argument("--cpu", type=int, help="Pin benchmark process to one logical CPU when supported.")
    parser.add_argument("--disable-gc", action=argparse.BooleanOptionalAction, default=False, help="Disable cyclic GC only inside each timed serialize call.")
    parser.add_argument("--use-float64", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--store-strings-as-bytes", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--java-verifier", type=Path, help="Path to encoding-verifier.jar.")
    parser.add_argument("--measure-memory", action="store_true", help="Run a separate tracemalloc pass for every structure/variant.")
    parser.add_argument("--profile-structure", action="append", default=[], help="Structure ID to cProfile; repeatable.")
    parser.add_argument("--profile-variant", action="append", default=[], help="Variant to cProfile; repeatable.")
    parser.add_argument(
        "--variants",
        default="pr_current,float_bytearray_reference,short_exact,selector_cached_exact,specialized_single_scale_exact,float_pipeline_exact,fixedpoint_one_pass_exact,delta_prealloc_exact,runlength_early_abort_exact,numeric_hotloops_exact,bytearray_repeat_exact,bytearray_array_exact,fast_integer_pack_exact,reuse_encoder_exact,all_exact,all_exact_repeat,best2_empirical,single_ip,single_rle,single_delta,single_delta_rle",
        help="Comma-separated strategy names.",
    )
    args = parser.parse_args()

    if args.write_manifest_template:
        write_manifest_template(args.write_manifest_template.expanduser().resolve())
        print(f"Wrote manifest template: {args.write_manifest_template}")
        return 0
    if args.manifest is None:
        parser.error("--manifest is required unless --write-manifest-template is used")
    if args.runs < 3:
        parser.error("Use at least 3 measured runs; 7 or more is recommended")
    if args.warmups < 0:
        parser.error("--warmups cannot be negative")

    repo = args.repo.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve()
    results_dir = args.results_dir.expanduser()
    if not results_dir.is_absolute():
        results_dir = (repo / results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.timing_root:
        timing_root = args.timing_root.expanduser().resolve()
    elif Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK):
        timing_root = Path("/dev/shm") / f"bcif_speed_optimizations_{os.getpid()}"
    else:
        timing_root = results_dir / "timing_tmp"
    timing_root.mkdir(parents=True, exist_ok=True)
    saved_root = results_dir / "outputs"
    saved_root.mkdir(parents=True, exist_ok=True)

    entries = parse_manifest(manifest)
    validate_paths(entries)
    if args.java_verifier is not None:
        args.java_verifier = args.java_verifier.expanduser().resolve()
        if not args.java_verifier.is_file():
            raise FileNotFoundError(args.java_verifier)

    sys.path.insert(0, str(repo))
    module = importlib.import_module("mmcif.io.BinaryCifWriter")
    source_path = Path(module.__file__).resolve()
    required = "_BinaryCifEncoders__encodeBestFixedPointChain"
    if not hasattr(module.BinaryCifEncoders, required):
        raise RuntimeError(
            f"{source_path} does not contain {required}. Check out the float-chain PR branch before running this harness."
        )

    patcher = PatchManager(module)
    variants = build_variants(module, patcher.originals)
    selected = [name.strip() for name in args.variants.split(",") if name.strip()]
    unknown = [name for name in selected if name not in variants]
    if unknown:
        raise ValueError(f"Unknown variants: {unknown}. Available: {sorted(variants)}")
    if "pr_current" not in selected:
        selected.insert(0, "pr_current")

    affinity = set_cpu_affinity(args.cpu)
    print(f"Repository: {repo}")
    print(f"Writer:     {source_path}")
    print(f"Results:    {results_dir}")
    print(f"Timing I/O: {timing_root}")
    print(f"Affinity:   {affinity}")
    print(f"Variants:   {', '.join(selected)}")

    print("\nLoading dictionaries...")
    dictionaries: dict[tuple[Path, ...], Any] = {}
    for key in dict.fromkeys(entry.dictionary_paths for entry in entries):
        dictionaries[key] = load_dictionary_api(key)
        print("  loaded", ";".join(str(p) for p in key))

    print("\nCIF inputs will be parsed one structure at a time.")
    print("Disk-backed clone caches will be released before the next structure.")
    if args.measure_memory or args.profile_structure:
        raise RuntimeError(
            "The streaming-safe runner intentionally disables --measure-memory and "
            "--profile-structure during the full-corpus pass. Run those diagnostics "
            "later with a one-structure manifest."
        )
    providers: dict[str, ContainerProvider] = {}
    clone_cache_root = timing_root / "clone_cache"
    clone_cache_root.mkdir(parents=True, exist_ok=True)

    metadata = collect_metadata(args, repo, source_path, affinity, entries, selected)
    (results_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, default=str) + "\n", encoding="utf-8")
    write_tsv(
        results_dir / "variants.tsv",
        [
            {
                "variant": variants[name].name,
                "description": variants[name].description,
                "expected_byte_identical": variants[name].expected_byte_identical,
                "expected_semantic_identical": variants[name].expected_semantic_identical,
                "tradeoff": variants[name].tradeoff,
            }
            for name in selected
        ],
    )

    try:
        runs = benchmark(module, patcher, variants, selected, entries, dictionaries, timing_root, saved_root, clone_cache_root, args)
        run_rows = [dataclasses.asdict(run) for run in runs]
        write_tsv(results_dir / "runs.tsv", run_rows)

        print("\nValidating generated outputs...")
        validations = validate_outputs(variants, selected, entries, saved_root, args.java_verifier, results_dir)
        write_tsv(results_dir / "validation.tsv", validations)

        memory_rows = run_memory_diagnostics(module, patcher, variants, selected, entries, providers, dictionaries, results_dir, args)
        if memory_rows:
            write_tsv(results_dir / "memory.tsv", memory_rows)

        run_profiles(module, patcher, variants, selected, entries, providers, dictionaries, results_dir, args)

        summary, aggregate = summarize_runs(runs, validations)
        write_tsv(results_dir / "summary.tsv", summary)
        write_tsv(results_dir / "aggregate.tsv", aggregate)
        write_report(results_dir / "report.md", variants, selected, aggregate, summary, validations, metadata)

        print("\nAggregate ranking")
        for row in sorted(aggregate, key=lambda r: float(r["sum_median_wall_seconds"])):
            print(
                f"  {row['variant']:34s} {float(row['sum_median_wall_seconds']):10.6f} s  "
                f"{float(row['aggregate_wall_faster_percent_vs_pr']):+9.3f}% faster  "
                f"{float(row['aggregate_size_change_percent_vs_pr']):+10.6f}% size  "
                f"byte={'Y' if row['all_byte_identical_to_pr'] else 'N'} semantic={'Y' if row['all_semantic_identical_to_pr'] else 'N'}"
            )
        print(f"\nComplete report: {results_dir / 'report.md'}")
        return 0
    finally:
        patcher.reset()
        if not args.timing_root:
            shutil.rmtree(timing_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
