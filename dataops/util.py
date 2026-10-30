from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

UID_RE = re.compile(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))+")
RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{8}")


def valid_uid(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and UID_RE.fullmatch(value) is not None


IO_WORKERS = int(os.environ.get("DATAOPS_IO_WORKERS") or 16)


def pmap(fn, items, workers: int = IO_WORKERS) -> list:
    """fn over items on a thread pool, results in input order. On NFS every open and stat is a round trip
    to the server, so reading many small files one at a time is latency-bound; 16 at once is ~10x faster."""
    items = list(items)
    if workers <= 1 or len(items) < 2:
        return [fn(i) for i in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


PROCS = int(os.environ.get("DATAOPS_PROCS") or min(10, os.cpu_count() or 1))


def _pmap_chunk(job: tuple) -> list:
    fn, chunk, threads = job
    return pmap(fn, chunk, threads)


def ppmap(fn, items, procs: int = PROCS, threads: int = 8, chunk: int = 64) -> list:
    """pmap over worker processes, for per-file work that is CPU-bound under the GIL (DICOM header parsing):
    chunks of items go to processes, each reading its chunk with a few threads. fn must be module-level
    and items picklable. Small inputs stay in this process."""
    items = list(items)
    if procs <= 1 or len(items) < 2 * chunk:
        return pmap(fn, items)
    from concurrent.futures import ProcessPoolExecutor
    chunks = [items[i:i + chunk] for i in range(0, len(items), chunk)]
    out = []
    with ProcessPoolExecutor(min(procs, len(chunks))) as ex:
        for part in ex.map(_pmap_chunk, [(fn, c, threads) for c in chunks]):
            out += part
    return out


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_hashed(src: Path, dst: Path) -> str:
    """Copy bytes and return the sha256 of what was written (read back from dst)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return sha256_file(dst)


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256_json(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    """Atomic write (temp file + rename) so a crash never leaves half a journal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def safe_name(s: str, n: int = 48) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s).strip("._")
    return (keep or "x")[:n]
