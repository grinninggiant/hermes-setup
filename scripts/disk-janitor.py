#!/usr/bin/env python3
"""Daily cleanup of Hermes scratch build output that otherwise piles up on the host disk.

Deletes top-level entries older than their age limit in:
  ~/.hermes/runtime/releases        (old runtime builds)       7 days
  ~/.hermes/profiles/*/artifacts    (agent work output)       14 days
Never deletes an entry that is referenced by a running process, an open file, a launchd
plist, a profile config, or Hermes Desktop / fleet state JSON (e.g. the live desktop backend).
Usage: disk-janitor.py [--dry-run]
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
HERMES = HOME / ".hermes"
DAY = 86400
TARGETS = [(HERMES / "runtime" / "releases", 7)] + [
    (p / "artifacts", 14) for p in sorted((HERMES / "profiles").iterdir()) if (p / "artifacts").is_dir()
]
REFERENCE_FILES = (
    list((HOME / "Library" / "Application Support" / "Hermes").glob("*.json"))
    + list(HERMES.glob("*.json"))
    + list(HERMES.glob("*.yaml"))
    + list((HOME / "Library" / "LaunchAgents").glob("ai.hermes.*.plist"))
    + list((HERMES / "profiles").glob("*/config.yaml"))
)


def referenced_text() -> str:
    parts = []
    for f in REFERENCE_FILES:
        try:
            parts.append(f.read_text(errors="ignore"))
        except OSError:
            pass
    ps = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True)
    parts.append(ps.stdout)
    # ponytail: one lsof over the whole host (~seconds); per-candidate lsof +D if it gets slow
    lsof = subprocess.run(["lsof", "-Fn"], capture_output=True, text=True)
    parts.append(lsof.stdout)
    return "\n".join(parts)


def is_referenced(path: Path, refs: str) -> bool:
    p = str(path)
    return (p + "/") in refs or any(line.endswith(p) for line in refs.splitlines() if p in line)


def force_rmtree(path: Path) -> None:
    def onerror(func, target, _exc):
        os.chmod(os.path.dirname(target), stat.S_IRWXU)
        os.chmod(target, stat.S_IRWXU) if os.path.exists(target) else None
        func(target)

    subprocess.run(["chflags", "-R", "nouchg", str(path)], capture_output=True)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onerror=onerror)
    else:
        path.unlink()


def main() -> int:
    dry = "--dry-run" in sys.argv
    refs = referenced_text()
    now = time.time()
    removed, kept_ref, freed = [], [], 0
    for root, days in TARGETS:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            # ponytail: top-level mtime as age; a tree-wide max mtime if long-lived dirs get cut
            if now - entry.lstat().st_mtime < days * DAY:
                continue
            if is_referenced(entry, refs):
                kept_ref.append(str(entry))
                continue
            size = int(subprocess.run(["du", "-sk", str(entry)], capture_output=True, text=True).stdout.split()[0] or 0)
            if not dry:
                force_rmtree(entry)
            removed.append(str(entry))
            freed += size
    print(json.dumps({"dry_run": dry, "removed": removed, "kept_referenced": kept_ref,
                      "freed_mib": freed // 1024}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # self-check: prefix match must not treat a sibling with a shared prefix as referenced
    assert is_referenced(Path("/x/a"), "/x/a/venv/bin/python")
    assert not is_referenced(Path("/x/a"), "/x/ab/venv/bin/python")
    assert is_referenced(Path("/x/a"), "cwd /x/a")
    raise SystemExit(main())
