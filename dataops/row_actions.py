"""What the 데이터셋 page offers for one row (CONTRACT §102): 파일럿 보내기, 로컬 SCP로 보내기, 샘플 보기.

A row of the one list (`dataset_list`) is mapped to the source folder this server can read and to the intake run of
that folder, if there is one. Each button is either possible — with the destination and the number of subjects the
confirmation names — or not, with the reason and the next step. Nothing here starts an analysis or a transfer: the
web's confirmation POST submits the existing job (`pilot`, or `local` = the CLI's local-only flow) after checking the
same conditions again; the sender's local-host / AE guard (§78.2) still decides at send time. Never Central.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from . import nfs
from .jobs import active_job
from .local_send import destination
from .retention import holds_images, slot_holders
from .runs import Run, RunError, list_runs

PILOT_STATES = ("PLANNED", "PILOT_READY", "PILOT_SENT", "PILOT_PARTIAL", "PILOT_FAILED")
LOCAL_FROM_PLAN = ("PLANNED",)
LOCAL_FROM_FREEZE = ("PREPARED", "LOCAL_SENT", "LOCAL_PARTIAL", "LOCAL_FAILED", "CENTRAL_SENT", "CENTRAL_PARTIAL",
                     "CENTRAL_FAILED")
WHAT = {"pilot": "파일럿 보내기", "local": "로컬 SCP로 보내기", "samples": "샘플 보기"}


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", (s or "").casefold())


def _primary(path: str) -> str:
    p = str(path or "").rstrip("/")
    for a, b in nfs.mirrors():
        if p == b or p.startswith(b + "/"):
            return a + p[len(b):]
    return p


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def source_folders(row: dict) -> list[str]:
    """Confirmed, present source folders of the row, in the catalogue's order."""
    return [m["folder"]["path"] for m in row.get("folders", [])
            if m["state"] == "confirmed" and m["folder"].get("present") and m["folder"].get("role") == "source"]


def row_runs(row: dict, runs: list[dict]) -> list[dict]:
    """The intake runs of this row, newest first: a run whose folder is one of the row's folders, or inside one and
    named in the row's name (Decathlon/Task02_Heart for "Decathlon (Task02_Heart)"), or a run of a registered
    dataset of the row."""
    folders = source_folders(row)
    uuids = {d["dataset_uuid"] for d in row.get("datasets", [])}
    run_ids = {r["run_uuid"] for d in row.get("datasets", []) for r in d.get("runs", [])}
    name = _norm(row.get("name", "")) + "".join(_norm(e["name"]) for e in row.get("entries", []))
    out = []
    for m in runs:
        root = _primary(m.get("input_root", ""))
        leaf = _norm(Path(root).name)
        hit = m.get("run_uuid") in run_ids or (m.get("dataset_uuid") in uuids and uuids) or any(
            root == f or (_inside(root, f) and len(leaf) >= 4 and leaf in name) for f in folders)
        if hit:
            out.append(m)
    return sorted(out, key=lambda m: m.get("created_at") or m["run_id"], reverse=True)


def pick_run(runs: list[dict]) -> dict | None:
    live = [m for m in runs if m.get("status") != "ANALYSIS_FAILED"]
    return (live or runs or [None])[0]


def sample_root(row: dict, run: dict | None) -> str | None:
    """The folder 샘플 보기 reads: the run's folder, else a sub-folder named in the row's name, else the first
    confirmed source folder."""
    if run and nfs.is_dir(run["input_root"]):
        return run["input_root"]
    folders = source_folders(row)
    if not folders:
        return None
    name = _norm(row.get("name", ""))
    for f in folders:
        try:
            subs = sorted(nfs.listdir(f))
        except OSError:
            continue
        for s in subs:
            if len(_norm(s)) >= 5 and _norm(s) in name and nfs.is_dir(str(Path(f) / s)):
                return str(Path(f) / s)
    return folders[0]


def is_internal(row: dict, settings, root: str | None) -> bool:
    if row.get("eff", {}).get("origin") == "internal":
        return True
    return bool(root) and any(_inside(_primary(root), _primary(r)) for r in settings.internal_roots)


class Context:
    """What every row of one page shares: the runs, the image slot, the local SCP guard, the viewer's role."""

    def __init__(self, settings, hub: dict | None):
        self.settings = settings
        self.runs = list_runs(settings.runs_dir)
        self.holders = slot_holders(settings.runs_dir)
        self.admin = not hub or hub.get("role") == "admin"
        self.scp_error = None
        if settings.local_scp is None:
            self.scp_error = "로컬 SCP가 설정되지 않았습니다"
        else:
            try:
                destination(settings.local_scp)
            except RunError as e:
                self.scp_error = str(e)
        self.dest = settings.local_scp.key if settings.local_scp else "-"


def _no(why: str, next_label: str | None = None, next_url: str | None = None) -> dict:
    return {"ok": False, "why": why, "next": (next_label, next_url) if next_label else None}


def _subjects_active(plan: dict) -> int:
    return len({s["subject_key"] for s in plan.get("series", []) if not s.get("excluded") and s["kind"] != "rtstruct"})


def actions(row: dict, ctx: Context, resolve_root: bool = True) -> dict:
    """{"pilot" | "local" | "samples": {"ok", "why", "next", "subjects", "dest", "run_id", "root"}}. Without
    `resolve_root` (the list page: no NAS listing per row) 샘플 보기 only says whether there is something to read."""
    s = ctx.settings
    runs = row_runs(row, ctx.runs)
    run_meta = pick_run(runs)
    folders = source_folders(row)
    root = sample_root(row, run_meta) if resolve_root else (
        (run_meta or {}).get("input_root") or (folders[0] if folders else None))
    out = {}

    # --- 샘플 보기 (a read: anyone who sees the page)
    if root:
        out["samples"] = {"ok": True, "why": "", "next": None, "root": root}
    else:
        out["samples"] = _no("이 서버에서 읽을 원본 폴더가 없습니다 (받지 않았거나 폴더가 연결되지 않음)",
                             "목록 항목에서 폴더 연결", None)

    def send_common() -> dict | None:
        if not ctx.admin:
            return _no("허브 관리자만 보낼 수 있습니다 (보기는 그대로)")
        if not folders and not run_meta:
            return _no("이 서버에 원본 폴더가 없습니다 — 먼저 NAS에 받고 목록 항목에서 폴더를 연결하세요")
        if run_meta is None:
            target = folders[0]
            return _no("아직 분석(plan)이 없습니다 — 버튼은 분석을 시작하지 않습니다", "새 분석에서 이 폴더 분석",
                       f"/new?dir={quote(str(Path(target).parent))}")
        if ctx.scp_error:
            return _no(f"로컬 SCP 설정이 거부됩니다: {ctx.scp_error}")
        return None

    def send_busy() -> dict | None:
        if active_job(s, run_meta["run_id"]):
            return _no("이 run의 작업이 진행 중입니다", "run 보기", f"/runs/{run_meta['run_id']}")
        other = [h for h in ctx.holders if h["run_id"] != run_meta["run_id"]]
        if other:
            o = other[0]
            return _no(f"다른 run이 이 서버의 영상 자리를 쓰고 있습니다: {o['run_id']} ({o.get('dataset_name') or '-'}, "
                       f"{o['status']}) — 한 번에 한 데이터셋만 (§78.6)", "그 run의 영상 삭제", f"/runs/{o['run_id']}#images")
        return None

    # --- 파일럿 보내기
    blocked = send_common()
    if blocked:
        out["pilot"] = blocked
    else:
        run = Run(s.runs_dir, run_meta["run_id"])
        st = run_meta["status"]
        plan = run.load("plan.json") or {}
        subjects = ((plan.get("pilot") or {}).get("subjects")) or []
        if st not in PILOT_STATES:
            out["pilot"] = _no(f"파일럿 단계가 아닙니다 (run 상태 {st})", "run 보기", f"/runs/{run.id}")
        elif not subjects:
            out["pilot"] = _no("파일럿 대상자를 아직 고르지 않았습니다", "run에서 대상자 고르기", f"/runs/{run.id}#pilot")
        else:
            out["pilot"] = send_busy() or {"ok": True, "why": "", "next": None, "run_id": run.id,
                                           "subjects": len(subjects), "subject_keys": subjects, "dest": ctx.dest}

    # --- 로컬 SCP로 보내기 (the whole dataset)
    blocked = send_common()
    if blocked:
        out["local"] = blocked
    else:
        run = Run(s.runs_dir, run_meta["run_id"])
        st = run_meta["status"]
        if st in LOCAL_FROM_PLAN:
            plan = run.load("plan.json") or {}
            out["local"] = send_busy() or {"ok": True, "why": "", "next": None, "run_id": run.id,
                                           "subjects": _subjects_active(plan), "dest": ctx.dest, "from_plan": True}
        elif st in LOCAL_FROM_FREEZE and run.load("manifest.json") and holds_images(run.dir):
            man = run.load("manifest.json")
            out["local"] = send_busy() or {"ok": True, "why": "", "next": None, "run_id": run.id, "dest": ctx.dest,
                                           "subjects": len({x["subject_key"] for x in man.get("series", [])}),
                                           "from_plan": False}
        elif st in LOCAL_FROM_FREEZE:
            out["local"] = _no(f"전체 변환본이 이 서버에 없습니다 (run 상태 {st}, 영상은 Central에 있고 이 서버에서는 지워짐)",
                               "run 보기", f"/runs/{run.id}")
        elif st in PILOT_STATES:
            out["local"] = _no(f"파일럿을 시작한 run은 승인 → Central 경로를 따릅니다 (상태 {st}); 로컬 전체 전송은 "
                               "검토만 끝난(PLANNED) run에서", "run 보기", f"/runs/{run.id}")
        else:
            out["local"] = _no(f"보낼 수 있는 상태가 아닙니다 (run 상태 {st})", "run 보기", f"/runs/{run.id}")
    for v in out.values():
        v.setdefault("run_id", run_meta["run_id"] if run_meta else None)
    return out
