#!/usr/bin/env python3
"""Progress of a run's conversion / send, read from outside: touches nothing the job uses.

    python3 ops/transfer_progress.py RUN_ID [--watch SECONDS]

The same reading the run page shows (dataops/progress.py, standard library only).
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from dataops.progress import job_progress  # noqa: E402

RUNS, JOBS = REPO / "runtime" / "runs", REPO / "runtime" / "jobs"


def latest_job(run_id: str) -> dict | None:
    jobs = [json.loads(p.read_text()) for p in JOBS.glob("*.json")]
    jobs = [j for j in jobs if j.get("run_id") == run_id]
    return max(jobs, key=lambda j: j["created_at"]) if jobs else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--watch", type=float, default=0)
    a = ap.parse_args()
    while True:
        meta = json.loads((RUNS / a.run_id / "run.json").read_text())
        job = latest_job(a.run_id)
        p = job_progress(RUNS / a.run_id, job, JOBS) if job else None
        line = [time.strftime("%H:%M:%S"), meta["status"]]
        if job:
            line.append(f"작업 {job['kind']} {job['status']}")
        if p:
            line.append(f"{p['text']}" + (f" ({p['pct']}%)" if p.get("pct") is not None else ""))
        print(" · ".join(line), flush=True)
        if not a.watch or not p:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    sys.exit(main())
