#!/usr/bin/env python3
"""Progress of a running Central / local send, read from outside: touches nothing the job uses.

    python3 ops/transfer_progress.py RUN_ID [--watch SECONDS]

Conversion: the job log's "[n/N]" lines. Sending: bytes the receiver acknowledged on the worker's open
association(s) to the target (``ss -tni``), against the sum of the frozen files of manifest.json, plus what the
transfer journal already records as accepted. The journal is written only before and after a whole send, so the
TCP counter is what moves during it.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runtime" / "runs"
JOBS = REPO / "runtime" / "jobs"


def latest_job(run_id: str) -> dict | None:
    jobs = [json.loads(p.read_text()) for p in JOBS.glob("*.json")]
    jobs = [j for j in jobs if j.get("run_id") == run_id]
    return max(jobs, key=lambda j: j["created_at"]) if jobs else None


def conversion(job: dict) -> str | None:
    log = JOBS / f"{job['id']}.log"
    if not log.is_file():
        return None
    hits = re.findall(r"\[(\d+)/(\d+)\]", log.read_text(errors="replace")[-4000:])
    return f"변환 {hits[-1][0]}/{hits[-1][1]} 시리즈" if hits else None


def acked(host: str, port: int) -> tuple[int, int]:
    """(bytes acknowledged, connections) over the established connections to host:port."""
    out = subprocess.run(["ss", "-tni", "state", "established", "dst", f"{host}:{port}"],
                         capture_output=True, text=True).stdout
    vals = [int(v) for v in re.findall(r"bytes_acked:(\d+)", out)]
    return sum(vals), len(vals)


def show(run_id: str, prev: tuple | None) -> tuple:
    run = RUNS / run_id
    meta = json.loads((run / "run.json").read_text())
    job = latest_job(run_id)
    line = [f"{time.strftime('%H:%M:%S')} {meta['status']}"]
    if job:
        line.append(f"작업 {job['kind']} {job['status']}")
        if job["status"] == "running" and (c := conversion(job)):
            line.append(c)
    manifest = run / "manifest.json"
    target = (job or {}).get("params", {}).get("target", "")
    m = re.match(r"\w+:[^@]+@([^:]+):(\d+)", target)
    if manifest.is_file() and m:
        man = json.loads(manifest.read_text())
        total = sum(i.get("size", 0) for i in man["instances"])
        stem = target.replace(":", "_").replace("@", "_")          # transfers/<target>.json (pilot_ = samples)
        journal = run / "transfers" / f"{stem}.json"
        journal = journal if journal.is_file() else None
        done_files = 0
        if journal:
            j = json.loads(journal.read_text())
            done_files = sum(1 for v in j["instances"].values() if v.get("status") in ("accepted", "warning"))
        b, n = acked(m.group(1), int(m.group(2)))
        line.append(f"파일 {len(man['instances']):,}개 {total / 1e9:.1f} GB")
        if n:
            rate = ""
            if prev and prev[0] <= b:
                mbps = (b - prev[0]) / max(time.time() - prev[1], 1e-6) / 1e6
                left = (total - b) / (mbps * 1e6) if mbps > 0 else 0
                rate = f", {mbps:.1f} MB/s, 남은 시간 약 {left / 60:.0f}분" if mbps > 0 else ""
            line.append(f"전송 중 {b / 1e9:.2f} GB ({100 * b / total:.1f}%){rate}")
        line.append(f"기록상 수신 {done_files:,}개")
        prev = (b, time.time())
    print(" · ".join(line), flush=True)
    return prev


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--watch", type=float, default=0)
    a = ap.parse_args()
    prev = None
    while True:
        prev = show(a.run_id, prev)
        if not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    sys.exit(main())
