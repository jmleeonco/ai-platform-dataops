"""Background jobs started from the browser: analyze a folder, pilot (2 cases -> local SCP), publish.

The web process only writes job files (runtime/jobs/<id>.json); one worker process (`python -m dataops
worker`, started by deploy/web.sh) runs them one at a time and appends progress to <id>.log. A job that
was running when the worker stopped is marked failed on the next start (its run keeps its last state;
every step is safe to repeat).
"""
from __future__ import annotations

import fcntl
import os
import secrets
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from .config import Settings
from .runs import Run, RunError
from .util import now_iso, read_json, write_json

KINDS = ("analyze", "sample", "pilot", "publish")
ACTIVE = ("queued", "running")


def dataset_uuid_for(input_root: str) -> str:
    """One dataset per input folder: re-analysing the same folder keeps the same dataset identity."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:ai-platform-dataops:dataset:" + input_root))


def check_input_root(settings: Settings, path: str) -> Path:
    p = Path((path or "").strip())
    if not p.is_absolute():
        raise RunError("입력 폴더는 절대 경로여야 합니다")
    p = p.resolve()
    roots = [Path(r).resolve() for r in settings.input_roots]
    if not any(p == r or p.is_relative_to(r) for r in roots):
        raise RunError(f"허용된 입력 위치({', '.join(map(str, roots))}) 밖의 폴더입니다")
    if p.name.lower() in ("backup", "backups") or any(part.startswith(".") for part in p.parts[1:]):
        raise RunError("backup/숨김 폴더는 분석하지 않습니다")
    if not p.is_dir():
        raise RunError(f"폴더가 없습니다: {p}")
    return p


def _path(settings: Settings, job_id: str) -> Path:
    return settings.jobs_dir / f"{job_id}.json"


def list_jobs(settings: Settings, run_id: str | None = None, limit: int = 50) -> list[dict]:
    d = settings.jobs_dir
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        j = read_json(p)
        if j and (run_id is None or j.get("run_id") == run_id):
            out.append(j)
        if len(out) >= limit:
            break
    return out


def active_job(settings: Settings, run_id: str) -> dict | None:
    return next((j for j in list_jobs(settings, run_id) if j["status"] in ACTIVE), None)


def submit(settings: Settings, kind: str, run_id: str, params: dict | None = None, by: str = "") -> dict:
    if kind not in KINDS:
        raise RunError(f"알 수 없는 작업 {kind}")
    if active_job(settings, run_id):
        raise RunError("이 run에 이미 대기/실행 중인 작업이 있습니다")
    job = {"id": f"{datetime.now():%Y%m%dT%H%M%S%f}-{secrets.token_hex(3)}-{kind}", "kind": kind, "run_id": run_id,
           "params": params or {}, "status": "queued", "created_at": now_iso(), "requested_by": by[:64],
           "started_at": None, "finished_at": None, "error": None, "result": None}
    write_json(_path(settings, job["id"]), job)
    return job


def start_analysis(settings: Settings, input_root: str, by: str = "") -> tuple[Run, dict]:
    root = check_input_root(settings, input_root)
    run = Run.create(settings.runs_dir, dataset_uuid_for(str(root)), str(root))
    run.set_status("ANALYZING", f"분석 요청{f' ({by})' if by else ''}")
    return run, submit(settings, "analyze", run.id, {"input_root": str(root)}, by)


def job_log(settings: Settings, job_id: str, n: int = 200) -> list[str]:
    p = settings.jobs_dir / f"{job_id}.log"
    if not p.is_file():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


# --------------------------------------------------------------------------- worker
def worker(settings: Settings, once: bool = False, poll_s: float = 2.0) -> None:
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    lock = open(settings.jobs_dir / ".worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RunError("다른 worker가 이미 실행 중입니다") from None
    lock.write(str(os.getpid()))
    lock.flush()
    for j in list_jobs(settings, limit=10_000):
        if j["status"] == "running":
            j.update(status="failed", finished_at=now_iso(), error="worker가 작업 중에 중지됨 — 다시 실행하세요")
            write_json(_path(settings, j["id"]), j)
            _release_run_lock(settings, j["run_id"])
    while True:
        queued = sorted((j for j in list_jobs(settings, limit=10_000) if j["status"] == "queued"),
                        key=lambda j: j["created_at"])
        if queued:
            run_job(settings, queued[0])
        elif once:
            return
        else:
            time.sleep(poll_s)


def _release_run_lock(settings: Settings, run_id: str) -> None:
    """The worker is the only process that runs long steps; its stale lock after a crash is removed."""
    try:
        lock = Run(settings.runs_dir, run_id).path(".lock")
        pid = int(lock.read_text().split()[0]) if lock.is_file() else None
    except (RunError, ValueError, IndexError, OSError):
        return
    if pid is None:
        return
    try:
        os.kill(pid, 0)                 # still alive: not ours to remove
    except ProcessLookupError:
        lock.unlink(missing_ok=True)
    except PermissionError:
        pass


def run_job(settings: Settings, job: dict) -> dict:
    path = _path(settings, job["id"])
    logf = settings.jobs_dir / f"{job['id']}.log"

    def log(msg: str) -> None:
        with open(logf, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {msg}\n")

    job.update(status="running", started_at=now_iso())
    write_json(path, job)
    try:
        run = Run(settings.runs_dir, job["run_id"])
        job["result"] = {"analyze": _analyze, "sample": _sample, "pilot": _pilot, "publish": _publish}[job["kind"]](settings, run, job, log)
        job.update(status="done")
        log("done")
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the job and the run
        job.update(status="failed", error=f"{type(e).__name__}: {e}"[:4000])
        log(f"FAILED: {e}")
        log(traceback.format_exc()[-4000:])
        if job["kind"] == "analyze":
            try:
                Run(settings.runs_dir, job["run_id"]).set_status("ANALYSIS_FAILED", str(e)[:500])
            except RunError:
                pass
    job["finished_at"] = now_iso()
    write_json(path, job)
    return job


def _analyze(settings: Settings, run: Run, job: dict, log) -> dict:
    from .agent import run_agent
    from .plan import validate_plan
    from .scan import scan
    from .spec import build_spec_plan
    root = Path(job["params"]["input_root"])
    if run.meta["status"] != "ANALYZING":
        run.set_status("ANALYZING", "재분석")
    log(f"scan {root}")
    s = scan(root)
    run.save("scan.json", s)
    log(f"scan: {s['totals']} in {s['dir_count']} directories"
        + (f" (stopped after {s['stopped_after_s']} s: partial)" if s.get("stopped_after_s") else ""))
    out = run_agent(settings, settings.agent_workdir / run.id, s, log)
    run.save("agent_proposal.json", out)
    log("spec: checking the proposal and building the plan (NIfTI headers only)")
    plan, inv = build_spec_plan(root, out["proposal"], run.id, run.meta["dataset_uuid"], out["meta"], log)
    run.save("inventory.json", inv)
    plan, errors = validate_plan(plan, inv)
    meta = run.store_plan(plan, errors, f"분석 완료 (에이전트 {out['meta'].get('model')}, ${out['meta'].get('cost_usd')})")
    log(f"plan: {len(plan['series'])} series, {len(errors)} errors, status {meta['status']}")
    from .metadata import nifti_headers
    log(f"metadata: {len(nifti_headers(run, plan, refresh=True))} NIfTI headers extracted")
    if meta["status"] == "PLANNED" and (plan.get("pilot") or {}).get("subjects"):
        try:                       # only the sample subjects are converted now; the rest after approval
            _sample(settings, run, job, log)
        except RunError as e:
            log(f"samples not extracted: {e}")
    return {"series": len(plan["series"]), "errors": len(errors), "status": run.meta["status"]}


def _sample(settings: Settings, run: Run, job: dict, log) -> dict:
    from .prepare import prepare
    m = prepare(run, "pilot", log)
    log(f"samples extracted: {len(m['instances'])} instances of {len(m['series'])} series")
    return {"instances": len(m["instances"]), "series": len(m["series"])}


def _pilot(settings: Settings, run: Run, job: dict, log) -> dict:
    from .local_send import send_local
    from .prepare import prepare
    m = prepare(run, "pilot", log)
    log(f"pilot frozen: {len(m['instances'])} instances; sending to {settings.local_scp.key}")
    r = send_local(run, settings.local_scp, scope="pilot")
    log(f"pilot send: {r['counts']} -> {r['status']}")
    return r


def _publish(settings: Settings, run: Run, job: dict, log) -> dict:
    from . import db as dbmod
    from .prepare import prepare
    from .publish import central_ready, publish_central
    ok, why = central_ready(settings.central)
    if not ok:
        raise RunError(why)
    if run.meta["status"] == "APPROVED":
        m = prepare(run, "full", log)
        log(f"full freeze: {len(m['instances'])} instances")
    log(f"sending to {settings.central.key}")
    r = publish_central(run, settings)
    log(f"central: {r['counts']} -> {r['status']}")
    if settings.db_configured:
        with run.lock(), dbmod.connection(settings.db) as conn:
            st = dbmod.migration_status(conn)
            if st["pending"] or st["changed"]:
                raise RunError(f"DB schema가 최신이 아닙니다 ({st})")
            r["registered"] = dbmod.register_run(conn, run)
        log(f"DB registered: {r['registered']}")
    else:
        log("DB 설정 없음 — 등록 생략")
    return r
