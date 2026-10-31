"""Reading inputs on a flaky NFS mount.

The 254 server behind /mnt2 sometimes answers "No such file" for files that exist (LOOKUP fails for a few
minutes, `ls` shows `d?????????`, then it recovers by itself). The 138 server exports the same data read-only
at /mnt2-v3-test. Every read of an input goes through `call`: it tries the path as given and, when that fails
the way the outage fails, reads the same file on the mirror, or waits for the primary to come back.

The path recorded in plans, manifests and the DB stays the primary one; only where the bytes are read from
changes. Bytes read from the mirror are checked against the hashes taken at planning time like any other read
(prepare.py), so a mirror that differs stops the run instead of slipping through.
"""
from __future__ import annotations

import errno
import os
import stat as stat_module
import sys
import time
from pathlib import Path

# A lookup that fails during the outage: no such file, stale handle, I/O error, "not a directory" for a
# directory whose entry the server lost.
TRANSIENT = {errno.ENOENT, errno.ESTALE, errno.EIO, errno.ENOTDIR}
# Waiting for the primary when there is no mirror copy; the outages seen lasted up to ~5 minutes.
WAITS = (1, 2, 5, 10, 20, 30, 60, 60, 60, 60)
DEFAULT_MIRRORS = "/mnt2/data=/mnt2-v3-test/data"


def mirrors() -> list[tuple[str, str]]:
    """[(primary prefix, mirror prefix)] from DATAOPS_INPUT_MIRRORS ("a=b:c=d"; empty turns it off)."""
    spec = os.environ.get("DATAOPS_INPUT_MIRRORS", DEFAULT_MIRRORS)
    out = []
    for part in spec.split(":"):
        a, _, b = part.partition("=")
        if a and b:
            out.append((a.rstrip("/"), b.rstrip("/")))
    return out


def mirror(path) -> Path | None:
    p = str(path)
    for a, b in mirrors():
        if p == a or p.startswith(a + "/"):
            return Path(b + p[len(a):])
    return None


def _note(msg: str) -> None:
    print(f"[nfs] {msg}", file=sys.stderr, flush=True)


def on_nfs_input(path) -> bool:
    """Under a primary or mirror prefix: worth waiting for. Anything else (local disk) fails at once."""
    p = str(path)
    return any(p == x or p.startswith(x + "/") for pair in mirrors() for x in pair)


def call(fn, path, *args, **kwargs):
    """fn(path, ...); when the NFS answers as if the file were gone: fn(mirror copy, ...), and if neither
    answers, both again after waits (an outage of either server). A file still missing on both after the
    waits raises the original error; a path outside the NFS inputs fails at once."""
    try:
        return fn(path, *args, **kwargs)
    except OSError as e:
        if e.errno not in TRANSIENT or not on_nfs_input(path):
            raise
        first = e
    alt = mirror(path)
    for wait in (0, *WAITS):
        if wait:
            _note(f"{path}: {first.strerror}; retrying in {wait}s")
            time.sleep(wait)
        for where in ((path, alt) if wait else (alt,)):
            if where is None:
                continue
            try:
                out = fn(where, *args, **kwargs)
            except OSError as e:
                if e.errno not in TRANSIENT:
                    raise
                continue
            if where != path:
                _note(f"{path}: {first.strerror} on the primary, read from {where}")
            return out
    raise first


def listdir(path) -> list[str]:
    return call(os.listdir, path)


def lstat(path):
    return call(os.lstat, path)


def stat(path):
    return call(os.stat, path)


def _exists(path) -> Path:
    os.stat(path)
    return Path(path)


def readable(path) -> Path:
    """The path to open for a reader that swallows its own errors (the NIfTI converter): the primary when it
    answers, else the mirror copy."""
    return call(_exists, path)


def _stat_now(path):
    """stat of the primary, or of the mirror copy when the primary answers as if it were gone; None if neither
    has it. No waiting: used for checks where a missing path is a normal answer (a path the agent proposed)."""
    try:
        return os.stat(path)
    except OSError as e:
        alt = mirror(path) if e.errno in TRANSIENT else None
        if alt is None:
            return None
        try:
            st = os.stat(alt)
        except OSError:
            return None
        _note(f"{path}: {e.strerror} on the primary, found on {alt}")
        return st


def exists(path) -> bool:
    return _stat_now(path) is not None


def is_dir(path) -> bool:
    st = _stat_now(path)
    return st is not None and stat_module.S_ISDIR(st.st_mode)


def is_file(path) -> bool:
    st = _stat_now(path)
    return st is not None and stat_module.S_ISREG(st.st_mode)
