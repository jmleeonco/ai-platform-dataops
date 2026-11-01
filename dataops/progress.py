"""Progress of a run's active job, read from outside the job: nothing here touches what the job uses.

Conversion: the job log's "[n/N]" lines. Sending: the frozen files' bytes (manifest) against what the receiver
has acknowledged on the open association(s) to the target (`ss -tni`, same user as the worker), plus what the
transfer journal records as received. Standard library only, so `ops/transfer_progress.py` runs without the venv.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

_last: dict[str, tuple[int, float]] = {}       # run id -> (bytes acked, monotonic time) of the previous reading


def _log_step(log: Path) -> tuple[int, int] | None:
    if not log.is_file():
        return None
    with log.open("rb") as f:
        f.seek(max(0, log.stat().st_size - 4000))
        hits = re.findall(rb"\[(\d+)/(\d+)\]", f.read())
    return (int(hits[-1][0]), int(hits[-1][1])) if hits else None


def _acked(host: str, port: int) -> tuple[int, int]:
    """(bytes acknowledged, connections) over the established connections to host:port."""
    try:
        out = subprocess.run(["ss", "-tni", "state", "established", "dst", f"{host}:{port}"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0
    vals = [int(v) for v in re.findall(r"bytes_acked:(\d+)", out)]
    return sum(vals), len(vals)


def job_progress(run_dir: Path, job: dict, jobs_dir: Path) -> dict | None:
    """{"phase", "pct", "text"} for a queued/running job, or None."""
    if not job or job.get("status") not in ("queued", "running"):
        return None
    if job["status"] == "queued":
        return {"phase": "queued", "pct": 0, "text": "대기 중"}
    target = (job.get("params") or {}).get("target") or ""
    m = re.match(r"\w+:[^@]+@([^:]+):(\d+)$", target)
    scope = "pilot" if job["kind"] == "pilot" else "full"
    manifest = run_dir / ("manifest-pilot.json" if scope == "pilot" else "manifest.json")
    step = _log_step(jobs_dir / f"{job['id']}.log")
    # sending once the manifest of this job's scope is newer than the job's start (otherwise still converting)
    sending = m and manifest.is_file() and job.get("started_at") and \
        manifest.stat().st_mtime >= time.mktime(time.strptime(job["started_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    if not sending:
        if step:
            n, total = step
            return {"phase": "convert", "pct": round(100 * n / total, 1), "text": f"변환 {n:,}/{total:,} 시리즈"}
        return {"phase": job["kind"], "pct": None, "text": "진행 중"}
    man = json.loads(manifest.read_text(encoding="utf-8"))
    files = len(man["instances"])
    total = sum(i.get("size", 0) for i in man["instances"]) or 1
    stem = target.replace(":", "_").replace("@", "_")
    journal = run_dir / "transfers" / f"{'pilot_' if scope == 'pilot' else ''}{stem}.json"
    received = 0
    if journal.is_file():
        j = json.loads(journal.read_text(encoding="utf-8"))
        if j.get("manifest_sha256") == man.get("manifest_sha256"):
            received = sum(1 for v in j["instances"].values() if v.get("status") in ("accepted", "warning"))
    if received >= files:
        return {"phase": "register", "pct": 100.0, "text": f"전송 완료 · 파일 {files:,}개 수신 · 관리 DB 등록 중", "target": target}
    b, conns = _acked(m.group(1), int(m.group(2)))
    now, key = time.monotonic(), str(run_dir)
    rate = None
    prev = _last.get(key)
    if prev and b >= prev[0] and now - prev[1] >= 1:
        rate = (b - prev[0]) / (now - prev[1])
    if not prev or now - prev[1] >= 2:
        _last[key] = (b, now)
    sent = max(b, 0)
    pct = round(min(100.0, 100 * max(sent / total, received / max(files, 1))), 1)
    text = f"전송 {sent / 1e9:.2f} / {total / 1e9:.2f} GB · 파일 {files:,}개"
    if received:
        text += f" · 수신 기록 {received:,}개"
    if rate:
        left = (total - sent) / rate if rate > 0 else 0
        text += f" · {rate / 1e6:.1f} MB/s · 남은 시간 약 {left / 60:.0f}분"
    if not conns:
        text += " · 연결 준비 중"
    return {"phase": "send", "pct": pct, "text": text, "target": target}
