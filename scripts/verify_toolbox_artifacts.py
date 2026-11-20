#!/usr/bin/env python3
"""Verify packaged Toolbox binaries without downloading or executing them."""

from __future__ import annotations

import argparse
import json
from native_security import inspect_binary_bytes, check_floors
import os
import re
import stat
import sys
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

MAX_BINARY_BYTES = 150 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_CHECKSUM_BYTES = 16 * 1024
EXPECTED_MACHINES = {
    "bin/linux-amd64/toolbox": 62,
    "bin/linux-arm64/toolbox": 183,
}


@contextmanager
def _regular_input(path: Path, limit: int) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"not a regular file: {path}")
        if metadata.st_size > limit:
            raise ValueError(f"artifact exceeds {limit} bytes: {path}")
        yield handle, metadata


def _read_text(path: Path, limit: int, *, encoding: str = "utf-8") -> str:
    with _regular_input(path, limit) as (handle, metadata):
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"artifact exceeds {limit} bytes: {path}")
    if len(data) != metadata.st_size:
        raise ValueError(f"artifact size changed during verification: {path}")
    return data.decode(encoding)


def _read_binary(path: Path, machine: int) -> bytes:
    limit = MAX_BINARY_BYTES - 1
    with _regular_input(path, limit) as (handle, metadata):
        header = handle.read(20)
        if (
            len(header) != 20
            or header[:5] != b"\x7fELF\x02"
            or header[5] != 1
            or int.from_bytes(header[18:20], "little") != machine
        ):
            raise ValueError(f"unexpected ELF architecture: {path}")
        if not metadata.st_mode & 0o111:
            raise ValueError(f"artifact is not executable: {path}")
        remainder = handle.read(limit + 1 - len(header))
    if len(header) + len(remainder) > limit:
        raise ValueError(f"artifact exceeds {limit} bytes: {path}")
    if len(header) + len(remainder) != metadata.st_size:
        raise ValueError(f"artifact size changed during verification: {path}")
    return header + remainder


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument(
        "--checksums",
        type=Path,
        required=True,
        help="Independently trusted binary SHA-256 manifest outside the artifact root (JSON or SHA256SUMS)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = args.root.resolve()
    checksum_path = args.checksums.resolve()
    if checksum_path.is_relative_to(root):
        print("[FAIL] trusted checksums must be outside the artifact root", file=sys.stderr)
        return 1
    failures: list[str] = []
    rows: dict[str, str] = {}
    package_target = ""
    package_metadata_path = root / ".fs-package.yaml"
    if package_metadata_path.is_file():
        try:
            import yaml

            package_metadata = yaml.safe_load(_read_text(package_metadata_path, MAX_METADATA_BYTES))
        except (ImportError, OSError, ValueError) as exc:
            print(f"[FAIL] cannot read package metadata: {exc}", file=sys.stderr)
            return 1
        except yaml.YAMLError as exc:
            print(f"[FAIL] cannot read package metadata: {exc}", file=sys.stderr)
            return 1
        if not isinstance(package_metadata, dict):
            print("[FAIL] package metadata must be an object", file=sys.stderr)
            return 1
        package_target = str(package_metadata.get("runtime_target") or "").strip()
        if package_target not in {"linux-amd64", "linux-arm64"}:
            print(f"[FAIL] unsupported runtime target: {package_target!r}", file=sys.stderr)
            return 1
    try:
        manifest_text = _read_text(checksum_path, MAX_CHECKSUM_BYTES, encoding="ascii")
        if checksum_path.suffix == ".json":
            expected = json.loads(manifest_text)["files"]
            lines = [
                f"{expected[relative]}  {relative}"
                for relative in EXPECTED_MACHINES
                if relative in expected
            ]
        else:
            lines = manifest_text.splitlines()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[FAIL] cannot read checksums: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        try:
            checksum, relative = line.split("  ", 1)
        except ValueError:
            failures.append(f"malformed checksum row: {line!r}")
            continue
        if relative in rows or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            failures.append(f"invalid or duplicate checksum for: {relative}")
            continue
        rows[relative] = checksum
    unexpected_rows = set(rows) - set(EXPECTED_MACHINES)
    if unexpected_rows:
        failures.extend(
            f"unexpected artifact path: {relative}" for relative in sorted(unexpected_rows)
        )
    if package_target:
        target_relative = f"bin/{package_target}/toolbox"
        present_artifacts = {
            path.relative_to(root).as_posix()
            for path in (root / "bin").rglob("*")
            if path.is_file()
        }
        if present_artifacts != {target_relative}:
            failures.append(
                f"target package must contain exactly {target_relative}; found {sorted(present_artifacts)}"
            )
        if target_relative not in rows:
            failures.append(f"checksum missing: {target_relative}")
        active_rows = {target_relative: rows[target_relative]} if target_relative in rows else {}
    else:
        if set(rows) != set(EXPECTED_MACHINES):
            failures.append("SHA256SUMS must cover exactly linux-amd64 and linux-arm64")
        active_rows = rows
    sizes: dict[str, int] = {}
    for relative, checksum in active_rows.items():
        expected_machine = EXPECTED_MACHINES.get(relative)
        if expected_machine is None:
            continue
        path = root / relative
        try:
            if path.resolve() != path:
                raise ValueError(f"artifact path must not contain a symbolic link: {relative}")
            blob = _read_binary(path, expected_machine)
            record = inspect_binary_bytes(blob, source=relative)
            sizes[relative] = len(blob)
            if record["sha256"] != checksum:
                failures.append(f"checksum mismatch: {relative}")
            baseline = json.loads(
                _read_text(root / "scripts/native-security-baseline.json", MAX_METADATA_BYTES)
            )
            check_floors(record, baseline)
            provenance = json.loads(
                _read_text(root / "toolbox-provenance.json", MAX_METADATA_BYTES)
            )
            manifest_record = provenance["binaries"][relative.split("/")[1]]
            for key in (
                "go_version",
                "modules",
                "module_gaps",
                "module_replacements",
                "sha256",
                "size",
            ):
                if manifest_record.get(key) != record[key]:
                    failures.append(f"provenance {key} differs from binary: {relative}")
        except (ValueError, OSError, KeyError, TypeError, AttributeError, struct.error) as exc:
            failures.append(f"native security metadata: {relative}: {exc}")
    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}", file=sys.stderr)
        return 1
    for relative in sorted(active_rows):
        print(f"[PASS] {relative} bytes={sizes[relative]} sha256={active_rows[relative]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
