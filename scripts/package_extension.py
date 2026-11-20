#!/usr/bin/env python3
"""Build deterministic, installer-compatible SQL target archives (stdlib only)."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

EXTENSION_ID = "flowsteward.data-connector-mcp-toolbox-mysql"
VERSION = "1.3.14"
TOP_FOLDER = "flowsteward_data_connector_mcp_toolbox_mysql"
SOURCE_DATE_EPOCH = 1779400183
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
MAX_GENERATED_METADATA_BYTES = 1024 * 1024
NATIVE_MANIFEST = Path("scripts/native-artifacts.json")
MACHINES = {"linux-amd64": 62, "linux-arm64": 183}
GENERATED_NATIVE_METADATA = (
    "toolbox-provenance.json",
    "SHA256SUMS",
    "packages.txt",
    "scripts/go-toolchain-selected.json",
)
PAYLOAD_DIRECTORIES = frozenset({"bin", "contracts", "ui", "scripts", "tests", "wheels"})
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".github",
        ".venv",
        "venv",
        ".fs-python",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "dev-wheels",
        "dist",
        "logs",
        "storage",
        ".cache",
    }
)
EXCLUDED_FILES = frozenset(
    {".fs-package.yaml", ".fs-extension-install.yaml", "live-oracle.local.json"}
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 64), b""):
            digest.update(block)
    return digest.hexdigest()


def _payload_paths(root: Path, target: str) -> list[Path]:
    paths = []
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED_DIRECTORIES and not (parent / name).is_symlink()
        )
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(root)
            if path.is_symlink() or name in EXCLUDED_FILES or name.endswith((".pyc", ".pyo")):
                continue
            if relative.parts[0] == "bin" and relative != Path("bin") / target / "toolbox":
                # Foreign targets are omitted; unexpected files in the selected target fail closed.
                if len(relative.parts) > 1 and relative.parts[1] == target:
                    raise ValueError("Target directory must contain exactly one native executable")
                continue
            if not path.is_file():
                raise ValueError(f"Package input is not a regular file: {relative}")
            paths.append(path)
    return sorted(paths)


def _freeze_source(root: Path, stage: Path) -> None:
    command = ["git", "--no-replace-objects", "-C", str(root)]
    try:
        repository = subprocess.check_output(  # nosec B603
            [*command, "rev-parse", "--show-toplevel"], text=True
        ).strip()
        if Path(repository).resolve() != root:
            raise ValueError("Release source must be the Git checkout root")
        revision = subprocess.check_output(  # nosec B603
            [*command, "rev-parse", "--verify", "HEAD^{commit}"], text=True
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Invalid release source commit")
        with tempfile.TemporaryFile() as snapshot:
            subprocess.run(  # nosec B603
                [*command, "archive", "--format=tar", revision], stdout=snapshot, check=True
            )
            snapshot.seek(0)
            with tarfile.open(fileobj=snapshot, mode="r:") as archive:
                for entry in archive:
                    relative = PurePosixPath(entry.name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError(f"Invalid release source path: {entry.name}")
                    if entry.isdir() or any(
                        part in EXCLUDED_DIRECTORIES for part in relative.parts
                    ):
                        continue
                    if relative.name in EXCLUDED_FILES:
                        continue
                    if not entry.isreg():
                        raise ValueError(f"Release source must be a regular file: {entry.name}")
                    destination = stage.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(entry)
                    if source is None:
                        raise ValueError(f"Missing release source bytes: {entry.name}")
                    with source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    destination.chmod(0o755 if entry.mode & 0o111 else 0o644)
    except subprocess.CalledProcessError as exc:
        raise ValueError("Release source must come from a frozen Git commit") from exc


def _copy_verified_native(root: Path, stage: Path, relative: str, expected: str) -> None:
    src = root / relative
    dst = stage / relative
    if src.resolve(strict=True) != src:
        raise ValueError(f"Generated release file must not use a symlink: {relative}")
    native = relative.startswith("bin/")
    limit = MAX_UNCOMPRESSED_BYTES - 1 if native else MAX_GENERATED_METADATA_BYTES
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(fd, "rb") as source, dst.open("xb") as output:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= limit:
            raise ValueError(f"Generated release file size or type is invalid: {relative}")
        if native and stat.S_IMODE(metadata.st_mode) != 0o755:
            raise ValueError("Native executable must have mode 0755")
        digest = hashlib.sha256()
        remaining = metadata.st_size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"Generated release file changed: {relative}")
            remaining -= len(chunk)
            digest.update(chunk)
            output.write(chunk)
        if source.read(1) or digest.hexdigest() != expected:
            raise ValueError(
                f"Generated release file does not match reviewed native manifest: {relative}"
            )
    dst.chmod(0o755 if native else 0o644)


def _reviewed_native_files(stage: Path) -> dict[str, str]:
    manifest = json.loads((stage / NATIVE_MANIFEST).read_text(encoding="utf-8"))
    expected = manifest["files"]
    filenames = {*GENERATED_NATIVE_METADATA, *(f"bin/{name}/toolbox" for name in MACHINES)}
    if (
        not isinstance(expected, dict)
        or set(expected) != filenames
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in expected.values()
        )
    ):
        raise ValueError("Invalid reviewed native manifest")
    return expected


def _stage_native(root: Path, stage: Path, target: str) -> None:
    expected = _reviewed_native_files(stage)
    for relative in (*GENERATED_NATIVE_METADATA, f"bin/{target}/toolbox"):
        _copy_verified_native(root, stage, relative, expected[relative])


def _native_metadata(root: Path, target: str) -> dict[str, str]:
    binary = root / "bin" / target / "toolbox"
    if any(path.is_symlink() for path in (root / "bin", binary.parent, binary)):
        raise ValueError("Native executable and parent directories must not be symlinks")
    if not binary.is_file() or stat.S_IMODE(binary.stat().st_mode) != 0o755:
        raise ValueError("Native executable must exist with mode 0755")
    with binary.open("rb") as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:5] != b"\x7fELF\x02" or header[5] not in (1, 2):
        raise ValueError("Native executable must be a valid ELF64 binary")
    machine = (
        int.from_bytes(header[18:20], "little")
        if header[5] == 1
        else int.from_bytes(header[18:20], "big")
    )
    if machine != MACHINES[target]:
        raise ValueError("Native executable ELF machine does not match runtime target")
    provenance_path = root / "toolbox-provenance.json"
    if provenance_path.is_symlink():
        raise ValueError("Provenance must not be a symlink")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    record = provenance["binaries"][target]
    # Packaging is a release boundary: validate actual linked metadata here,
    # even when an old payload has internally consistent hashes/provenance.
    spec = importlib.util.spec_from_file_location(
        "package_native_security", root / "scripts/native_security.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("Native security verifier is unavailable")
    security = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(security)
    actual = security.inspect_binary(binary)
    baseline = json.loads((root / "scripts/native-security-baseline.json").read_text())
    security.check_floors(actual, baseline)
    for key in ("go_version", "modules", "module_gaps", "module_replacements", "sha256", "size"):
        if record.get(key) != actual[key]:
            raise ValueError(f"Native executable {key} does not match provenance")
    digest = actual["sha256"]
    if record.get("sha256") != digest or record.get("size") != binary.stat().st_size:
        raise ValueError("Native executable SHA-256 or size does not match provenance")
    return {"path": f"bin/{target}/toolbox", "sha256": digest}


def _installer_metadata(target: str, digest: str) -> bytes:
    native = {"path": f"bin/{target}/toolbox", "sha256": digest}
    return (
        json.dumps({"runtime_target": target, "native_executable": native}, sort_keys=True).encode()
        + b"\n"
    )


def verify_archive(root: Path, target: str, package: Path) -> None:
    """Recheck every archive entry against the independently checked-out release tag."""
    if target not in MACHINES or not 0 < package.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid release archive size or runtime target")
    with tempfile.TemporaryDirectory(prefix="mysql-publish-source-") as scratch:
        stage = Path(scratch) / "source"
        _freeze_source(root.resolve(strict=True), stage)
        approved = _reviewed_native_files(stage)
        expected = {
            path.relative_to(stage).as_posix(): (
                sha256(path),
                0o755 if path.stat().st_mode & 0o111 else 0o644,
            )
            for path in _payload_paths(stage, target)
        }
        native = f"bin/{target}/toolbox"
        for relative in GENERATED_NATIVE_METADATA:
            expected[relative] = (approved[relative], 0o644)
        expected[native] = (approved[native], 0o755)
        data = _installer_metadata(target, approved[native])
        expected[".fs-package.yaml"] = (hashlib.sha256(data).hexdigest(), 0o644)
        canonical = Path(scratch) / "canonical.zip"
        _verify_archive_entries(package, canonical, expected)
        if sha256(canonical) != sha256(package):
            raise ValueError("Release archive encoding differs from the deterministic package")


def _verify_archive_entries(
    package: Path, canonical: Path, expected: dict[str, tuple[str, int]]
) -> None:
    with zipfile.ZipFile(package) as archive, zipfile.ZipFile(canonical, "x") as verified:
        entries = archive.infolist()
        paths = sorted(PurePosixPath(name) for name in expected if name != ".fs-package.yaml")
        names = [f"{TOP_FOLDER}/{path}" for path in paths]
        names.append(f"{TOP_FOLDER}/.fs-package.yaml")
        if [entry.filename for entry in entries] != names:
            raise ValueError("Release archive entries differ from the deterministic package")
        if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Release archive exceeds installer uncompressed-size limit")
        for entry in entries:
            prefix = f"{TOP_FOLDER}/"
            relative = entry.filename.removeprefix(prefix)
            member = expected.get(relative)
            if not entry.filename.startswith(prefix) or member is None:
                raise ValueError(f"Unexpected release entry: {entry.filename}")
            digest, mode = member
            if entry.external_attr >> 16 != (stat.S_IFREG | mode):
                raise ValueError(f"Unexpected release file mode: {relative}")
            with archive.open(entry) as stream:
                data = stream.read(entry.file_size + 1)
            if len(data) != entry.file_size or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"Release entry differs from reviewed input: {relative}")
            _write_entry(verified, relative, data, mode)


def _write_entry(archive: zipfile.ZipFile, relative: str, data: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(f"{TOP_FOLDER}/{relative}", time.gmtime(SOURCE_DATE_EPOCH)[:6])
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compresslevel=9)


def package_extension(root: Path, target: str, out_dir: Path) -> Path:
    """Verify a native payload, then atomically publish one target-specific ZIP."""
    if target not in MACHINES:
        raise ValueError("Unsupported runtime target")
    root = root.resolve(strict=True)
    out_dir = out_dir.resolve()
    if out_dir == root:
        raise ValueError("Output directory must differ from the extension root")
    if out_dir.is_relative_to(root) and out_dir.relative_to(root).parts[0] in PAYLOAD_DIRECTORIES:
        raise ValueError("Output directory must not overlap release payload directories")
    with tempfile.TemporaryDirectory(prefix="mysql-release-source-") as scratch:
        snapshot = Path(scratch)
        _freeze_source(root, snapshot)
        _stage_native(root, snapshot, target)
        return _package_source(snapshot, target, out_dir)


def _package_source(root: Path, target: str, out_dir: Path) -> Path:
    native = _native_metadata(root, target)
    paths = _payload_paths(root, target)
    for required in (
        "LICENSE",
        "NOTICE",
        "extension.yaml",
        "variant.json",
        *GENERATED_NATIVE_METADATA,
    ):
        if root / required not in paths:
            raise ValueError(f"Required release file missing: {required}")
    metadata = _installer_metadata(target, native["sha256"])
    if sum(path.stat().st_size for path in paths) + len(metadata) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("Extension archive exceeds installer uncompressed-size limit")
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / f"{EXTENSION_ID}-{VERSION}-{target}.zip"
    descriptor, temporary = tempfile.mkstemp(prefix=".package-", suffix=".zip", dir=out_dir)
    os.close(descriptor)
    pending = Path(temporary)
    try:
        with zipfile.ZipFile(pending, "w") as archive:
            for path in paths:
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                if data.startswith(b"\x7fELF") and relative != native["path"]:
                    raise ValueError("Package must contain exactly one native executable")
                if (
                    relative == native["path"]
                    and hashlib.sha256(data).hexdigest() != native["sha256"]
                ):
                    raise ValueError("Native executable changed during packaging")
                mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                _write_entry(archive, relative, data, mode)
            _write_entry(archive, ".fs-package.yaml", metadata, 0o644)
        if pending.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Extension archive exceeds installer compressed-size limit")
        with zipfile.ZipFile(pending) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Extension archive exceeds installer uncompressed-size limit")
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)
    return destination


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-target", required=True, choices=sorted(MACHINES))
    parser.add_argument("--out-dir", type=Path, default=root / "dist")
    parser.add_argument("--verify-archive", type=Path)
    args = parser.parse_args()
    try:
        if args.verify_archive is not None:
            verify_archive(root, args.runtime_target, args.verify_archive)
            print(f"[PASS] reviewed release source and native manifest: {args.verify_archive}")
            return 0
        archive = package_extension(root, args.runtime_target, args.out_dir)
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
        parser.exit(2, f"Packaging failed: {error}\n")
    (args.out_dir / "SHA256SUMS").write_text(
        "".join(
            f"{sha256(path)}  {path.name}\n"
            for path in sorted(args.out_dir.glob(f"{EXTENSION_ID}-{VERSION}-*.zip"))
        )
    )
    print(f"[PASS] packed: {archive}")
    print(f"[PASS] sha256: {sha256(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
