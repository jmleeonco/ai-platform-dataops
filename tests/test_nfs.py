"""Reading inputs through an NFS mount that intermittently answers "No such file" (dataops/nfs.py)."""
import errno
import hashlib
import os
import time

import pytest

from dataops import nfs
from dataops.inventory import inventory
from dataops.util import sha256_file


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    primary, mirror = tmp_path / "primary", tmp_path / "mirror"
    for root in (primary, mirror):
        (root / "ds" / "sub").mkdir(parents=True)
        (root / "ds" / "top.txt").write_text("top")
        (root / "ds" / "sub" / "a.txt").write_text("hello")
    monkeypatch.setenv("DATAOPS_INPUT_MIRRORS", f"{primary}={mirror}")
    monkeypatch.setattr(nfs, "WAITS", (0.01, 0.01))
    return primary, mirror


def outage(monkeypatch, dead):
    """The primary answers ENOENT for every path under `dead`, the way the 254 server does during an outage."""
    real_lstat, real_stat, real_listdir, real_open = os.lstat, os.stat, os.listdir, open

    def guard(fn):
        def wrapped(p, *a, **k):
            if str(p).startswith(str(dead)):
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(p))
            return fn(p, *a, **k)
        return wrapped
    monkeypatch.setattr(os, "lstat", guard(real_lstat))
    monkeypatch.setattr(os, "stat", guard(real_stat))
    monkeypatch.setattr(os, "listdir", guard(real_listdir))
    monkeypatch.setattr("builtins.open", guard(real_open))


def test_a_read_during_an_outage_uses_the_mirror(mounts, monkeypatch):
    primary, _ = mounts
    outage(monkeypatch, primary / "ds" / "sub")
    assert sha256_file(primary / "ds" / "sub" / "a.txt") == hashlib.sha256(b"hello").hexdigest()
    assert nfs.is_dir(primary / "ds" / "sub") and nfs.exists(primary / "ds" / "sub" / "a.txt")


def test_the_walk_does_not_lose_a_folder_during_an_outage(mounts, monkeypatch):
    """Before: a directory whose lookup failed was silently skipped (or taken for a file), so an analysis
    during an outage saw fewer files. Now the walk reads it from the mirror."""
    primary, _ = mounts
    outage(monkeypatch, primary / "ds" / "sub")
    files = {f["rel"]: f for f in inventory(primary / "ds")["files"]}
    assert set(files) == {"top.txt", "sub/a.txt"} and files["sub/a.txt"]["size"] == 5


def test_missing_on_both_mounts_is_an_error_after_the_waits(mounts):
    primary, _ = mounts
    t = time.monotonic()
    with pytest.raises(FileNotFoundError):
        sha256_file(primary / "ds" / "nope.txt")
    assert time.monotonic() - t < 5
    assert not nfs.exists(primary / "ds" / "nope.txt")


def test_local_files_fail_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(nfs, "WAITS", (60,))          # would hang if a local path were waited for
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "missing.bin")
