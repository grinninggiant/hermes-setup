#!/usr/bin/env python3
"""Shared verification and retention primitives for Hermes/Honcho backups."""

from __future__ import annotations

import argparse
import errno
import fnmatch
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable


def secure_directory(path: Path | str) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def secure_file(path: Path | str) -> Path:
    path = Path(path)
    path.chmod(0o600)
    return path


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256_manifest(path: Path | str) -> Path:
    path = Path(path)
    manifest = path.with_name(path.name + ".sha256")
    tmp = manifest.with_name("." + manifest.name + ".partial")
    tmp.write_text(f"{sha256_file(path)}  {path.name}\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, manifest)
    manifest.chmod(0o600)
    return manifest


def verify_gzip(path: Path | str) -> None:
    path = Path(path)
    try:
        with gzip.open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                pass
    except (OSError, EOFError) as exc:
        raise ValueError(f"invalid gzip archive: {path}: {exc}") from exc


def _verify_sqlite(path: Path) -> None:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    for attempt in range(3):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                rows = conn.execute("PRAGMA quick_check").fetchall()
            finally:
                conn.close()
            if rows != [("ok",)]:
                raise ValueError(f"SQLite quick_check failed for {path}: {rows[:5]}")
            return
        except sqlite3.OperationalError as exc:
            if attempt == 2:
                raise ValueError(f"SQLite open/check failed for {path}: {exc}") from exc
            time.sleep(0.5 * (attempt + 1))


def verify_hermes_zip(path: Path | str) -> dict:
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"not a zip archive: {path}")
    sqlite_files = 0
    sqlite_ok = 0
    with zipfile.ZipFile(path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"zip CRC failed: {bad}")
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            raise ValueError("empty Hermes archive")
        if not any(Path(name).name in {"config.yaml", ".env", "state.db"} for name in names):
            raise ValueError("archive has no Hermes marker file")
        with tempfile.TemporaryDirectory(dir=path.parent) as temp_dir:
            temp_root = Path(temp_dir)
            for name in names:
                if not name.endswith(".db"):
                    continue
                sqlite_files += 1
                target = temp_root / f"db-{sqlite_files}.db"
                with archive.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                _verify_sqlite(target)
                sqlite_ok += 1
    return {
        "archive": str(path),
        "files": len(names),
        "sqlite_files": sqlite_files,
        "sqlite_ok": sqlite_ok,
        "sha256": sha256_file(path),
    }


def verify_snapshot(path: Path | str) -> dict:
    path = Path(path)
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"snapshot manifest missing: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed = manifest.get("failed_dbs") or []
    oversized = manifest.get("oversized_skipped") or []
    if failed or oversized:
        raise ValueError(f"incomplete snapshot: failed_dbs={failed}, oversized={oversized}")
    checked = 0
    for relative, expected_size in (manifest.get("files") or {}).items():
        candidate = path / relative
        if not candidate.is_file():
            raise ValueError(f"snapshot file missing: {relative}")
        if candidate.stat().st_size != expected_size:
            raise ValueError(f"snapshot size mismatch: {relative}")
        if candidate.suffix == ".db":
            _verify_sqlite(candidate)
            checked += 1
    return {"snapshot": str(path), "files": len(manifest.get("files") or {}), "sqlite_ok": checked}


def _budget_status(used_bytes: int, thresholds: dict) -> str:
    for status in ("critical", "high", "warning"):
        if used_bytes >= thresholds[status]:
            return status
    return "ok"


_POLICY_FIELDS = {"schema_version", "allowed_roots", "classes"}
_CLASS_FIELDS = {
    "name",
    "root",
    "pattern",
    "kind",
    "keep_count",
    "min_age_days",
    "required_sidecars",
    "verification_sidecar",
    "protected_marker",
    "active_references",
    "disk_budget_bytes",
    "candidate_budget_level",
}
_BUDGET_FIELDS = {"warning", "high", "critical"}


def _identity(stat_result: os.stat_result) -> dict:
    return {"device": stat_result.st_dev, "inode": stat_result.st_ino}


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _schema_integer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _validate_policy(policy: dict) -> None:
    if not isinstance(policy, dict):
        raise ValueError("retention policy must be an object")
    unexpected = set(policy) - _POLICY_FIELDS
    if unexpected:
        raise ValueError(f"unexpected policy fields: {sorted(unexpected)}")
    if set(policy) != _POLICY_FIELDS:
        raise ValueError("retention policy fields are incomplete")
    if not _schema_integer(policy["schema_version"]) or policy["schema_version"] != 1:
        raise ValueError("unsupported retention policy schema_version")
    roots = policy["allowed_roots"]
    if not isinstance(roots, list) or not roots:
        raise ValueError("retention policy requires at least one allowed root")
    if any(
        not isinstance(root, str)
        or not os.path.isabs(root)
        or os.path.normpath(root) != root
        for root in roots
    ):
        raise ValueError("allowed roots must be absolute normalized paths")
    if len(set(roots)) != len(roots):
        raise ValueError("allowed roots must be unique")
    classes = policy["classes"]
    if not isinstance(classes, list) or not classes:
        raise ValueError("retention policy requires at least one class")
    for item in classes:
        if not isinstance(item, dict) or set(item) != _CLASS_FIELDS:
            raise ValueError("retention class fields do not match schema")
        for field in ("name", "root", "pattern", "verification_sidecar", "protected_marker"):
            if not isinstance(item[field], str) or not item[field]:
                raise ValueError(f"retention class {field} must be a non-empty string")
        if not isinstance(item["kind"], str) or item["kind"] not in {"file", "directory"}:
            raise ValueError(f"invalid artifact kind for {item['name']}")
        if not _schema_integer(item["keep_count"]) or item["keep_count"] < 1:
            raise ValueError(f"invalid keep_count for {item['name']}")
        if not _schema_integer(item["min_age_days"]) or item["min_age_days"] < 0:
            raise ValueError(f"invalid min_age_days for {item['name']}")
        thresholds = item["disk_budget_bytes"]
        if not isinstance(thresholds, dict) or set(thresholds) != _BUDGET_FIELDS:
            raise ValueError(f"disk budget fields do not match schema for {item['name']}")
        values = [thresholds[level] for level in ("warning", "high", "critical")]
        if any(not _schema_integer(value) or value < 1 for value in values) or not (
            values[0] < values[1] < values[2]
        ):
            raise ValueError(f"disk budget thresholds must increase for {item['name']}")
        if (
            not isinstance(item["candidate_budget_level"], str)
            or item["candidate_budget_level"] not in _BUDGET_FIELDS
        ):
            raise ValueError(f"invalid candidate budget level for {item['name']}")
        sidecars = item["required_sidecars"]
        if (
            not isinstance(sidecars, list)
            or not sidecars
            or any(not isinstance(value, str) or not value for value in sidecars)
            or len(set(sidecars)) != len(sidecars)
        ):
            raise ValueError(f"required sidecars are invalid for {item['name']}")
        references = item["active_references"]
        if (
            not isinstance(references, list)
            or any(not isinstance(value, str) or not value for value in references)
            or len(set(references)) != len(references)
        ):
            raise ValueError(f"active references are invalid for {item['name']}")
    names = [item["name"] for item in classes]
    if len(set(names)) != len(names):
        raise ValueError("retention class names must be unique")
    class_roots = [Path(item["root"]) for item in classes]
    for index, left in enumerate(class_roots):
        for right in class_roots[index + 1:]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError("retention class roots must not overlap")


def _safe_local_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and Path(value).name == value
    )


def _open_readonly(parent_fd: int, name: str, *, directory: bool = False) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return os.open(name, flags, dir_fd=parent_fd)


def _stat_nofollow(parent_fd: int, name: str) -> os.stat_result:
    result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(result.st_mode):
        raise ValueError(f"symlink:{name}")
    return result


def _open_absolute_directory(path: Path) -> tuple[int, list[tuple[int, os.stat_result, str]]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open("/", flags)
    guards = [(root_fd, os.fstat(root_fd), "/")]
    current_fd = root_fd
    try:
        for component in path.parts[1:]:
            initial = _stat_nofollow(current_fd, component)
            if not stat.S_ISDIR(initial.st_mode):
                raise ValueError(f"unexpected_root_component:{path}")
            next_fd = _open_readonly(current_fd, component, directory=True)
            opened = os.fstat(next_fd)
            if not _same_snapshot(initial, opened):
                os.close(next_fd)
                raise ValueError(f"identity_changed:{path}")
            guards.append((next_fd, opened, component))
            current_fd = next_fd
        return current_fd, guards
    except ValueError as exc:
        for guard_fd, _, _ in reversed(guards):
            os.close(guard_fd)
        if str(exc).startswith("symlink:"):
            raise ValueError(f"symlinked_root:{path}") from exc
        raise
    except OSError as exc:
        for guard_fd, _, _ in reversed(guards):
            os.close(guard_fd)
        if exc.errno == errno.ELOOP:
            raise ValueError(f"symlinked_root:{path}") from exc
        raise


def _validate_directory_chain(guards: list[tuple[int, os.stat_result, str]], path: Path) -> None:
    for index, (guard_fd, expected, component) in enumerate(guards):
        if not _same_snapshot(expected, os.fstat(guard_fd)):
            raise ValueError(f"identity_changed:{path}")
        if index == 0:
            continue
        parent_fd = guards[index - 1][0]
        current = _stat_nofollow(parent_fd, component)
        if not _same_snapshot(expected, current):
            raise ValueError(f"identity_changed:{path}")


def _close_directory_chain(guards: list[tuple[int, os.stat_result, str]]) -> None:
    for guard_fd, _, _ in reversed(guards):
        os.close(guard_fd)


def _validate_filesystem_authority(classes: list[dict]) -> None:
    seen = []
    for item in classes:
        path = Path(item["root"])
        if not path.is_absolute() or os.path.normpath(path) != str(path):
            continue
        guards = []
        overlap = False
        try:
            _, guards = _open_absolute_directory(path)
            chain = {(value.st_dev, value.st_ino) for _, value, _ in guards}
            endpoint = (guards[-1][1].st_dev, guards[-1][1].st_ino)
            overlap = any(
                endpoint in previous_chain or previous_endpoint in chain
                for previous_endpoint, previous_chain in seen
            )
            seen.append((endpoint, chain))
        except (OSError, ValueError):
            continue
        finally:
            if guards:
                _close_directory_chain(guards)
        if overlap:
            raise ValueError("retention class filesystem authority overlaps")


def _pin_regular_file(parent_fd: int, name: str, expected: os.stat_result) -> os.stat_result:
    file_fd = _open_readonly(parent_fd, name)
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"unexpected_file_type:{name}")
        if not _same_snapshot(expected, opened):
            raise ValueError(f"identity_changed:{name}")
        final = os.fstat(file_fd)
        if not _same_snapshot(opened, final):
            raise ValueError(f"identity_changed:{name}")
        return opened
    finally:
        os.close(file_fd)


def _validate_tree_fd(directory_fd: int, device: int, display_root: Path) -> int:
    total = 0
    with os.scandir(directory_fd) as entries:
        for entry in sorted(entries, key=lambda value: value.name):
            result = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(result.st_mode):
                raise ValueError(f"symlink:{display_root / entry.name}")
            if result.st_dev != device:
                raise ValueError(f"cross_filesystem:{display_root / entry.name}")
            if stat.S_ISDIR(result.st_mode):
                child_fd = _open_readonly(directory_fd, entry.name, directory=True)
                try:
                    opened = os.fstat(child_fd)
                    if not _same_snapshot(result, opened):
                        raise ValueError(f"identity_changed:{display_root / entry.name}")
                    total += _validate_tree_fd(child_fd, device, display_root / entry.name)
                    if not _same_snapshot(opened, os.fstat(child_fd)):
                        raise ValueError(f"identity_changed:{display_root / entry.name}")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(result.st_mode):
                total += _pin_regular_file(directory_fd, entry.name, result).st_size
            else:
                raise ValueError(f"unexpected_file_type:{display_root / entry.name}")
    return total


def _path_device_ids(root_fd: int, root: Path) -> set[int]:
    root_stat = os.fstat(root_fd)
    _validate_tree_fd(root_fd, root_stat.st_dev, root)
    return {root_stat.st_dev}


def _read_json_fd(parent_fd: int, name: str, expected: os.stat_result) -> dict:
    file_fd = _open_readonly(parent_fd, name)
    try:
        opened = os.fstat(file_fd)
        if not _same_snapshot(expected, opened):
            raise ValueError(f"identity_changed:{name}")
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"unexpected_sidecar_type:{name}")
        with os.fdopen(os.dup(file_fd), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not _same_snapshot(opened, os.fstat(file_fd)):
            raise ValueError(f"identity_changed:{name}")
    finally:
        os.close(file_fd)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid_verification_sidecar:{name}")
    return payload


def _sidecar_name(artifact_name: str, suffix: str, kind: str) -> str:
    return suffix if kind == "directory" else artifact_name + suffix


def _marker_identity_fd(parent_fd: int, name: str) -> dict | None:
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(result.st_mode):
        raise ValueError(f"symlink:{name}")
    if not stat.S_ISREG(result.st_mode):
        raise ValueError(f"unexpected_marker_type:{name}")
    return _identity(_pin_regular_file(parent_fd, name, result))


def _inspect_artifact(root_fd: int, root: Path, name: str, item: dict) -> dict:
    kind = item["kind"]
    initial = _stat_nofollow(root_fd, name)
    expected = stat.S_ISREG(initial.st_mode) if kind == "file" else stat.S_ISDIR(initial.st_mode)
    if not expected:
        raise ValueError(f"unexpected_artifact_type:{root / name}")
    artifact_fd = _open_readonly(root_fd, name, directory=kind == "directory")
    try:
        opened = os.fstat(artifact_fd)
        if not _same_snapshot(initial, opened):
            raise ValueError(f"identity_changed:{root / name}")
        parent_fd = artifact_fd if kind == "directory" else root_fd
        sidecar_suffixes = list(dict.fromkeys(
            [*item["required_sidecars"], item["verification_sidecar"]]
        ))
        sidecar_names = [_sidecar_name(name, suffix, kind) for suffix in sidecar_suffixes]
        sidecar_stats = []
        for sidecar_name in sidecar_names:
            sidecar_path = (root / name / sidecar_name) if kind == "directory" else (root / sidecar_name)
            try:
                sidecar_stat = _stat_nofollow(parent_fd, sidecar_name)
            except FileNotFoundError as exc:
                raise ValueError(f"missing_sidecar:{sidecar_path}") from exc
            if not stat.S_ISREG(sidecar_stat.st_mode):
                raise ValueError(f"unexpected_sidecar_type:{sidecar_name}")
            sidecar_stats.append(_pin_regular_file(parent_fd, sidecar_name, sidecar_stat))
        verification_name = _sidecar_name(name, item["verification_sidecar"], kind)
        verification_index = sidecar_names.index(verification_name)
        metadata = _read_json_fd(
            parent_fd, verification_name, sidecar_stats[verification_index]
        )
        marker_identity = _marker_identity_fd(
            parent_fd, _sidecar_name(name, item["protected_marker"], kind)
        )
        size = opened.st_size
        if kind == "directory":
            size = _validate_tree_fd(artifact_fd, opened.st_dev, root / name)
        else:
            size += sum(value.st_size for value in sidecar_stats)
        final = os.fstat(artifact_fd)
        if not _same_snapshot(opened, final):
            raise ValueError(f"identity_changed:{root / name}")
        return {
            "path": root / name,
            "mtime": opened.st_mtime,
            "size": size,
            "verified": metadata.get("verified") is True,
            "protected": marker_identity is not None,
            "protected_marker_identity": marker_identity,
            "identity": _identity(opened),
            "paired_paths": [
                str((root / name / suffix) if kind == "directory" else (root / (name + suffix)))
                for suffix in sidecar_suffixes
            ],
            "paired_identities": [
                {
                    "path": str((root / name / suffix) if kind == "directory" else (root / (name + suffix))),
                    **_identity(sidecar_stat),
                }
                for suffix, sidecar_stat in zip(sidecar_suffixes, sidecar_stats)
            ],
        }
    finally:
        os.close(artifact_fd)


def _scan_artifacts_fd(root_fd: int, root: Path, item: dict) -> list[dict]:
    root_stat = os.fstat(root_fd)
    if len(_path_device_ids(root_fd, root)) != 1:
        raise ValueError(f"cross_filesystem:{root}")
    artifacts = []
    with os.scandir(root_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        if fnmatch.fnmatchcase(name, item["pattern"]):
            artifacts.append(_inspect_artifact(root_fd, root, name, item))
    final_root = os.fstat(root_fd)
    if not _same_snapshot(root_stat, final_root):
        raise ValueError(f"identity_changed:{root}")
    return artifacts


def _blocked_class_report(name: str, root: Path, reason: str) -> dict:
    return {
        "name": name,
        "root": str(root),
        "root_identity": None,
        "artifact_count": 0,
        "verified_count": 0,
        "budget": {"used_bytes": 0, "thresholds": {}, "status": "unknown"},
        "artifacts": [],
        "candidates": [],
        "bytes_to_reclaim": 0,
        "blocked_reasons": [reason],
    }


def _inspection_reason(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid_verification_sidecar:{exc.msg}"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"inspection_failed:{exc}"


def build_retention_report(policy: dict, *, now_epoch: int | float) -> dict:
    """Build a deterministic, read-only candidate report from a retention policy."""
    _validate_policy(policy)
    _validate_filesystem_authority(policy["classes"])
    allowed_roots = set(policy["allowed_roots"])
    reports = []
    all_candidates = []
    for item in policy["classes"]:
        name = item["name"]
        root = Path(item["root"])
        pattern = item["pattern"]
        blocked_reason = None
        root_identity = None
        if not root.is_absolute() or os.path.normpath(root) != str(root):
            blocked_reason = f"unsafe_root:{root}"
        elif str(root) not in allowed_roots:
            blocked_reason = f"unexpected_root:{root}"
        elif Path(pattern).name != pattern or pattern in {"", ".", ".."}:
            blocked_reason = f"unsafe_pattern:{pattern}"
        else:
            sidecars = [*item["required_sidecars"], item["verification_sidecar"]]
            unsafe_sidecar = next((value for value in sidecars if not _safe_local_name(value)), None)
            if unsafe_sidecar is not None:
                blocked_reason = f"unsafe_sidecar:{unsafe_sidecar}"
            elif not _safe_local_name(item["protected_marker"]):
                blocked_reason = f"unsafe_protected_marker:{item['protected_marker']}"
        references = set()
        if blocked_reason is None:
            for reference in item["active_references"]:
                if (
                    not isinstance(reference, str)
                    or not os.path.isabs(reference)
                    or os.path.normpath(reference) != reference
                    or str(Path(reference).parent) != str(root)
                ):
                    blocked_reason = f"unsafe_active_reference:{reference}"
                    break
                references.add(reference)
        artifacts = []
        root_fd = None
        directory_chain = []
        if blocked_reason is None:
            try:
                root_fd, directory_chain = _open_absolute_directory(root)
                root_stat = os.fstat(root_fd)
                root_identity = _identity(root_stat)
                artifacts = _scan_artifacts_fd(root_fd, root, item)
                _validate_directory_chain(directory_chain, root)
                path_stat = root.stat(follow_symlinks=False)
                if not _same_snapshot(root_stat, path_stat):
                    raise ValueError(f"identity_changed:{root}")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                blocked_reason = _inspection_reason(exc)
            finally:
                if directory_chain:
                    _close_directory_chain(directory_chain)
        if blocked_reason is not None:
            reports.append(_blocked_class_report(name, root, blocked_reason))
            continue

        artifacts.sort(key=lambda value: (-value["mtime"], str(value["path"])))
        used_bytes = sum(value["size"] for value in artifacts)
        thresholds = item["disk_budget_bytes"]
        budget = {
            "used_bytes": used_bytes,
            "thresholds": thresholds,
            "status": _budget_status(used_bytes, thresholds),
            "candidate_level": item["candidate_budget_level"],
        }
        budget_allows_candidates = used_bytes >= thresholds[item["candidate_budget_level"]]
        verified_rank = 0
        candidates = []
        artifact_reports = []
        for artifact in artifacts:
            path = artifact["path"]
            age_seconds = max(0, int(now_epoch - artifact["mtime"]))
            referenced = str(path) in references
            if not artifact["verified"]:
                state = "unverified"
            elif artifact["protected"]:
                state = "protected"
            elif referenced:
                state = "active_reference"
            elif verified_rank < item["keep_count"]:
                state = "kept_count"
            elif age_seconds < item["min_age_days"] * 86400:
                state = "kept_age"
            elif not budget_allows_candidates:
                state = "kept_budget"
            else:
                state = "candidate"
                candidate = {
                    "class": name,
                    "path": str(path),
                    "identity": artifact["identity"],
                    "paired_paths": artifact["paired_paths"],
                    "paired_identities": artifact["paired_identities"],
                    "protected_marker_identity": artifact["protected_marker_identity"],
                    "age_seconds": age_seconds,
                    "size_bytes": artifact["size"],
                    "reasons": [
                        "outside_keep_count",
                        "older_than_min_age",
                        f"disk_budget_at_least_{item['candidate_budget_level']}",
                    ],
                }
                candidates.append(candidate)
            if artifact["verified"]:
                verified_rank += 1
            artifact_reports.append(
                {
                    "path": str(path),
                    "identity": artifact["identity"],
                    "paired_identities": artifact["paired_identities"],
                    "protected_marker_identity": artifact["protected_marker_identity"],
                    "verified": artifact["verified"],
                    "age_seconds": age_seconds,
                    "size_bytes": artifact["size"],
                    "state": state,
                }
            )
        report = {
            "name": name,
            "root": str(root),
            "root_identity": root_identity,
            "artifact_count": len(artifacts),
            "verified_count": sum(1 for value in artifacts if value["verified"]),
            "budget": budget,
            "artifacts": artifact_reports,
            "candidates": candidates,
            "bytes_to_reclaim": sum(value["size_bytes"] for value in candidates),
            "blocked_reasons": [],
        }
        reports.append(report)
        all_candidates.extend(candidates)
    return {
        "schema_version": 1,
        "generated_at_epoch": now_epoch,
        "classes": reports,
        "candidates": all_candidates,
        "bytes_to_reclaim": sum(value["size_bytes"] for value in all_candidates),
        "blocked": any(report["blocked_reasons"] for report in reports),
    }

def prune_to_count(paths: Iterable[Path | str], keep: int) -> list[Path]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    candidates = sorted((Path(path) for path in paths if Path(path).exists()), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for path in candidates[keep:]:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
            for suffix in (".sha256", ".meta.json"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
        removed.append(path)
    return removed


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("secure-dir", "secure-file", "write-sha256", "verify-gzip", "verify-hermes-zip", "verify-snapshot"):
        item = sub.add_parser(command)
        item.add_argument("path")
    prune = sub.add_parser("prune")
    prune.add_argument("pattern")
    prune.add_argument("--keep", type=int, required=True)
    snapshots = sub.add_parser("prune-snapshots")
    snapshots.add_argument("root")
    snapshots.add_argument("--keep", type=int, required=True)
    retention = sub.add_parser("retention-report")
    retention.add_argument("--policy", required=True)
    retention.add_argument("--now-epoch", type=float, required=True)
    args = parser.parse_args()

    if args.command == "secure-dir":
        _print({"path": str(secure_directory(args.path)), "mode": "0700"})
    elif args.command == "secure-file":
        _print({"path": str(secure_file(args.path)), "mode": "0600"})
    elif args.command == "write-sha256":
        _print({"manifest": str(write_sha256_manifest(args.path))})
    elif args.command == "verify-gzip":
        verify_gzip(args.path)
        _print({"archive": args.path, "gzip": "ok"})
    elif args.command == "verify-hermes-zip":
        _print(verify_hermes_zip(args.path))
    elif args.command == "verify-snapshot":
        _print(verify_snapshot(args.path))
    elif args.command == "prune":
        paths = list(Path().glob(args.pattern)) if not os.path.isabs(args.pattern) else list(Path(args.pattern).parent.glob(Path(args.pattern).name))
        _print({"removed": [str(p) for p in prune_to_count(paths, args.keep)]})
    elif args.command == "prune-snapshots":
        root = Path(args.root)
        candidates = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        _print({"removed": [str(p) for p in prune_to_count(candidates, args.keep)]})
    elif args.command == "retention-report":
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
        report = build_retention_report(policy, now_epoch=args.now_epoch)
        _print(report)
        return 2 if report["blocked"] else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
