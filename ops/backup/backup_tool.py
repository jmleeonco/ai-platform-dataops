#!/usr/bin/env python3
"""Helpers for nightly_backup.sh (stdlib only).

  backup_tool.py manifest WORK_DIR --started EPOCH --commit SHA --ok 1|0 [--error TEXT] [--extra JSON]
  backup_tool.py prune BASE_DIR --today YYYY-MM-DD [--dry-run]
  backup_tool.py verify-files DAY_DIR                       re-hash files listed in manifest.json
  backup_tool.py verify-artifacts SNAPSHOT_DIR              re-hash every artifact against each run's manifest

Retention (CONTRACT §65): the 14 most recent daily folders plus the first folder of each month for the
12 most recent months; today's folder is never deleted; only YYYY-MM-DD folders inside BASE_DIR are touched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
KEEP_DAILY = 14
KEEP_MONTHS = 12


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def keep_set(dates: list[str], today: str) -> set[str]:
    dates = sorted({d for d in dates if DATE_RE.fullmatch(d)}, reverse=True)
    keep = set(dates[:KEEP_DAILY]) | {today}
    first_of_month = {}
    for d in sorted(dates):
        first_of_month.setdefault(d[:7], d)
    for month in sorted(first_of_month, reverse=True)[:KEEP_MONTHS]:
        keep.add(first_of_month[month])
    return keep


def prune(base: Path, today: str, dry_run: bool) -> list[str]:
    base = base.resolve()
    names = [p.name for p in base.iterdir() if p.is_dir() and DATE_RE.fullmatch(p.name)]
    keep = keep_set(names, today)
    removed = []
    for name in sorted(set(names) - keep):
        target = (base / name).resolve()
        if target.parent != base:          # never outside the system's own folder
            continue
        removed.append(name)
        if not dry_run:
            shutil.rmtree(target)
    return removed


def manifest(work: Path, started: float, commit: str, ok: bool, error: str | None, extra: dict) -> dict:
    files = []
    for p in sorted(work.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files.append({"path": p.relative_to(work).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)})
    counts = {}
    tsv = work / "db" / "row_counts.tsv"
    if tsv.exists():
        for line in tsv.read_text().splitlines():
            if "\t" in line:
                t, n = line.split("\t", 1)
                counts[t] = int(n)
    m = {"system": "ai-platform-dataops", "schema": 1, "ok": ok, "error": error,
         "started_at": dt.datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds"),
         "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
         "duration_s": round(time.time() - started, 1), "commit": commit, "db_counts": counts, "files": files,
         **extra}
    (work / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return m


def verify_files(day: Path) -> list[str]:
    m = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
    bad = []
    for f in m["files"]:
        p = day / f["path"]
        if not p.is_file() or p.stat().st_size != f["size"] or sha256(p) != f["sha256"]:
            bad.append(f["path"])
    return bad


def verify_artifacts(snapshot: Path) -> tuple[int, list[str]]:
    n, bad = 0, []
    for man in sorted(snapshot.glob("*/manifest.json")):
        m = json.loads(man.read_text(encoding="utf-8"))
        for inst in m["instances"]:
            p = man.parent / inst["path"]
            n += 1
            if not p.is_file() or sha256(p) != inst["sha256"]:
                bad.append(f"{man.parent.name}/{inst['path']}")
    return n, bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("work", type=Path)
    m.add_argument("--started", type=float, required=True)
    m.add_argument("--commit", default="")
    m.add_argument("--ok", type=int, choices=[0, 1], required=True)
    m.add_argument("--error")
    m.add_argument("--extra", default="{}")
    p = sub.add_parser("prune")
    p.add_argument("base", type=Path)
    p.add_argument("--today", required=True)
    p.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("verify-files")
    v.add_argument("day", type=Path)
    a = sub.add_parser("verify-artifacts")
    a.add_argument("snapshot", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "manifest":
        res = manifest(args.work, args.started, args.commit, bool(args.ok), args.error, json.loads(args.extra))
        print(f"manifest: {len(res['files'])} files, ok={res['ok']}")
    elif args.cmd == "prune":
        removed = prune(args.base, args.today, args.dry_run)
        print(f"pruned {args.base}: {removed or 'nothing'}")
    elif args.cmd == "verify-files":
        bad = verify_files(args.day)
        print("files ok" if not bad else f"MISMATCH: {bad}")
        return 1 if bad else 0
    elif args.cmd == "verify-artifacts":
        n, bad = verify_artifacts(args.snapshot)
        print(f"artifacts checked {n}, mismatches {len(bad)}" + (f": {bad[:10]}" if bad else ""))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
