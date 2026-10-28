"""Run directories: runtime/runs/<run_id>/{run.json, inventory.json, plan.json, manifest.json, transfers/, artifacts/}."""
from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime
from pathlib import Path

from .util import RUN_ID_RE, now_iso, read_json, sha256_json, write_json

# Browser flow:  ANALYZING -> PLANNED/NEEDS_INPUT -> PILOT_READY -> PILOT_SENT -> APPROVED -> PREPARED
#                -> CENTRAL_SENT | CENTRAL_PARTIAL | CENTRAL_FAILED (publication may be retried).
# CLI flow:      PLANNED -> PREPARED -> LOCAL_SENT | LOCAL_PARTIAL | LOCAL_FAILED (local only, no approval).
# The plan may change until approval (a change makes the pilot stale); a prepared run is immutable.
EDITABLE = {None, "ANALYZING", "ANALYSIS_FAILED", "PLANNED", "NEEDS_INPUT", "PILOT_READY", "PILOT_SENT",
            "PILOT_PARTIAL", "PILOT_FAILED"}
_PLAN = {"PLANNED", "NEEDS_INPUT"}
_PILOT = {"PILOT_SENT", "PILOT_PARTIAL", "PILOT_FAILED"}
_LOCAL = {"LOCAL_SENT", "LOCAL_PARTIAL", "LOCAL_FAILED"}
_CENTRAL = {"CENTRAL_SENT", "CENTRAL_PARTIAL", "CENTRAL_FAILED"}
TRANSITIONS = {
    None: _PLAN | {"ANALYZING"},
    "ANALYZING": _PLAN | {"ANALYZING", "ANALYSIS_FAILED"},
    "ANALYSIS_FAILED": {"ANALYZING", "ANALYSIS_FAILED"},
    "PLANNED": _PLAN | {"PREPARED", "PILOT_READY", "ANALYZING"},
    "NEEDS_INPUT": _PLAN | {"ANALYZING"},
    "PILOT_READY": _PLAN | _PILOT | {"PILOT_READY"},
    "PILOT_SENT": _PLAN | _PILOT | {"PILOT_READY", "APPROVED"},
    "PILOT_PARTIAL": _PLAN | _PILOT | {"PILOT_READY"},
    "PILOT_FAILED": _PLAN | _PILOT | {"PILOT_READY"},
    "APPROVED": {"PREPARED", "APPROVED"},
    "PREPARED": _LOCAL | _CENTRAL,
    **{s: _LOCAL | _CENTRAL for s in _LOCAL | _CENTRAL},
}


class RunError(Exception):
    pass


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"


class Run:
    def __init__(self, runs_dir: Path, run_id: str):
        if not RUN_ID_RE.fullmatch(run_id or ""):
            raise RunError(f"잘못된 run id: {run_id!r}")
        self.id = run_id
        self.dir = runs_dir / run_id

    # ---- files ----
    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def meta(self) -> dict:
        m = read_json(self.path("run.json"))
        if m is None:
            raise RunError(f"run {self.id} 없음")
        return m

    def load(self, name: str, default=None):
        return read_json(self.path(name), default)

    def save(self, name: str, obj) -> None:
        write_json(self.path(name), obj)

    @property
    def exists(self) -> bool:
        return self.path("run.json").exists()

    # ---- lifecycle ----
    @classmethod
    def create(cls, runs_dir: Path, dataset_uuid: str, input_root: str, run_id: str | None = None) -> "Run":
        run = cls(runs_dir, run_id or new_run_id())
        if run.exists:
            raise RunError(f"run {run.id} 이미 있음")
        run.dir.mkdir(parents=True)
        run.save("run.json", {"run_id": run.id, "run_uuid": str(uuid.uuid4()), "status": None, "revision": 0,
                              "plan_sha256": None, "dataset_uuid": dataset_uuid, "input_root": input_root,
                              "created_at": now_iso(), "updated_at": now_iso(), "events": []})
        return run

    def set_status(self, status: str, event: str, **extra) -> dict:
        meta = self.meta
        if status not in TRANSITIONS.get(meta["status"], set()):
            raise RunError(f"상태 전이 불가: {meta['status']} -> {status}")
        meta.update(status=status, updated_at=now_iso(), **extra)
        meta["events"].append({"at": now_iso(), "status": status, "event": event})
        self.save("run.json", meta)
        return meta

    def store_plan(self, plan: dict, errors: list[str], event: str) -> dict:
        """Persist a (re)validated plan; a changed plan bumps the revision."""
        meta = self.meta
        if meta["status"] not in EDITABLE:
            raise RunError(f"{meta['status']} 상태의 run은 plan을 바꿀 수 없습니다 (승인/준비 이후에는 새 run을 만드세요)")
        digest = sha256_json(plan)
        self.save("plan.json", plan)
        self.save("plan_errors.json", errors)
        revision = meta["revision"] + (1 if digest != meta["plan_sha256"] else 0)
        return self.set_status("NEEDS_INPUT" if errors else "PLANNED", event, plan_sha256=digest, revision=revision)

    # ---- locking: one mutating command per run at a time ----
    def lock(self):
        return _RunLock(self.path(".lock"))


class _RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RunError(f"다른 작업이 이 run을 사용 중입니다 ({self.path}); 중단된 작업이면 파일을 확인 후 삭제") from None
        os.write(self.fd, f"{os.getpid()} {now_iso()}".encode())
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


def list_runs(runs_dir: Path) -> list[dict]:
    out = []
    if not runs_dir.is_dir():
        return out
    for d in sorted(runs_dir.iterdir(), reverse=True):
        if RUN_ID_RE.fullmatch(d.name) and (d / "run.json").exists():
            meta = read_json(d / "run.json")
            plan = read_json(d / "plan.json", {}) or {}
            meta["dataset_name"] = (plan.get("dataset") or {}).get("name")
            out.append(meta)
    return out
