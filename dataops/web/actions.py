"""Browser actions: start an analysis, edit the plan's reviewable fields, pilot, approve, publish.

Every POST needs the per-browser CSRF token (cookie + hidden field) and, when the browser sends one, an
Origin of this server. Long work is queued as a job for the worker; handlers only validate and record.
Edits go through validate_plan + Run.store_plan exactly like the CLI (`dataops validate`).
"""
from __future__ import annotations

import hmac
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import deid
from ..config import Settings
from ..jobs import active_job, check_input_root, start_analysis, submit
from ..plan import PREFIX_RE, validate_plan
from ..publish import approve, central_ready
from ..runs import EDITABLE, Run, RunError
from ..spec import SAMPLES
from ..util import sha256_json

CSRF_COOKIE = "dataops_csrf"
SPLIT_NAMES = {"train": "학습", "validation": "검증", "test": "테스트", "other": "기타", "none": "구분 없음"}


async def checked_form(request: Request) -> dict:
    form = dict(await request.form())
    token = request.cookies.get(CSRF_COOKIE, "")
    if not token or not hmac.compare_digest(token, str(form.get("csrf", ""))):
        raise HTTPException(403, "CSRF 토큰이 맞지 않습니다 — 페이지를 새로고침한 뒤 다시 시도하세요")
    origin = request.headers.get("origin")
    if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "다른 출처에서 온 요청입니다")
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in form.items()}


def back(run_id: str, msg: str = "", err: str = "", anchor: str = "") -> RedirectResponse:
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/runs/{run_id}{q}{('#' + anchor) if anchor else ''}", status_code=303)


def browse(settings: Settings, rel: str | None) -> dict:
    """Folders one level below an allowed input root (for the new-analysis picker)."""
    roots = [Path(r) for r in settings.input_roots]
    cur = None
    if rel:
        try:
            cur = check_input_root(settings, rel)
        except RunError:
            cur = None
    base = [cur] if cur else roots
    entries = []
    for b in base:
        try:
            for d in sorted(b.iterdir()):
                if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in ("backup", "backups"):
                    entries.append(str(d))
        except OSError:
            continue
    parent = str(cur.parent) if cur and any(cur != r and cur.is_relative_to(r) for r in roots) else None
    return {"current": str(cur) if cur else None, "entries": entries[:500], "parent": parent, "roots": roots}


def register_actions(app: FastAPI, settings: Settings, page, get_run) -> None:
    def editable(run: Run) -> None:
        meta = run.meta
        if meta["status"] not in EDITABLE or meta["status"] in ("ANALYZING", "ANALYSIS_FAILED"):
            raise RunError(f"{meta['status']} 상태에서는 plan을 바꿀 수 없습니다")
        if active_job(settings, run.id):
            raise RunError("작업이 진행 중입니다 — 끝난 뒤 수정하세요")

    def save_plan(run: Run, plan: dict, event: str) -> list[str]:
        inv = run.load("inventory.json")
        with run.lock():
            plan, errors = validate_plan(plan, inv)
            run.store_plan(plan, errors, event)
        return errors

    def result(run: Run, errors: list[str], ok: str, anchor: str = "") -> RedirectResponse:
        if errors:
            return back(run.id, err=f"저장했지만 plan 오류 {len(errors)}건: {errors[0]}", anchor=anchor)
        return back(run.id, msg=ok, anchor=anchor)

    @app.get("/new", response_class=HTMLResponse)
    def new_form(request: Request, dir: str | None = None, err: str | None = None):
        return page(request, "new.html", browse=browse(settings, dir), err=err)

    @app.post("/new")
    async def new_submit(request: Request):
        form = await checked_form(request)
        try:
            run, _ = start_analysis(settings, form.get("input_root", ""), form.get("by", ""), form.get("deid") == "1")
        except RunError as e:
            return RedirectResponse(f"/new?err={quote(str(e))}", status_code=303)
        return back(run.id, msg="분석을 시작했습니다. 폴더 크기에 따라 몇 분 걸립니다.")

    @app.post("/runs/{run_id}/dataset")
    async def edit_dataset(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            editable(run)
            plan = run.load("plan.json")
            ds = plan["dataset"]
            prefix = form.get("patient_id_prefix", "").upper()
            if prefix and not PREFIX_RE.fullmatch(prefix):
                raise RunError("PatientID 접두어는 [A-Za-z0-9_-] 1..32자")
            if form.get("commercial_use") not in ("Y", "N", "U"):
                raise RunError("상업적 이용은 Y/N/U")
            name = form.get("name", "")
            if not 1 <= len(name) <= 256:
                raise RunError("데이터셋 이름은 1..256자")
            ds.update(name=name, license=form.get("license") or None, source_url=form.get("source_url") or None,
                      description=form.get("description") or None, commercial_use=form["commercial_use"],
                      patient_id_prefix=prefix or None)
            errors = save_plan(run, plan, "데이터셋 정보 수정 (웹)")
        except RunError as e:
            return back(run.id, err=str(e), anchor="dataset")
        return result(run, errors, "데이터셋 정보를 저장했습니다", "dataset")

    @app.post("/runs/{run_id}/splits")
    async def edit_splits(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            editable(run)
            plan = run.load("plan.json")
            keep = {k[6:] for k in form if k.startswith("split_")}
            for s in plan["series"]:          # labels follow their image (validate_plan)
                if s["kind"] == "nifti":
                    s["excluded"] = (s.get("split") or "none") not in keep
            errors = save_plan(run, plan, f"포함 분할: {', '.join(sorted(keep)) or '없음'} (웹)")
        except RunError as e:
            return back(run.id, err=str(e), anchor="scope")
        return result(run, errors, "포함할 분할을 저장했습니다", "scope")

    @app.post("/runs/{run_id}/labels")
    async def edit_labels(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            editable(run)
            plan = run.load("plan.json")
            include = form.get("labels") == "on"
            for s in plan["series"]:
                if s["kind"] == "rtstruct":
                    ref_excluded = s.get("excluded_by_reference")
                    s["excluded"] = bool(ref_excluded) or not include
            errors = save_plan(run, plan, f"라벨 RTSTRUCT {'포함' if include else '제외'} (웹)")
        except RunError as e:
            return back(run.id, err=str(e), anchor="scope")
        return result(run, errors, "라벨 포함 여부를 저장했습니다", "scope")

    @app.post("/runs/{run_id}/modality")
    async def edit_modality(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            editable(run)
            if form.get("modality") not in ("CT", "MR"):
                raise RunError("modality는 CT 또는 MR")
            plan = run.load("plan.json")
            s = next((x for x in plan["series"] if x["series_key"] == form.get("series_key")
                      and x["kind"] == "nifti"), None)
            if s is None:
                raise RunError("시리즈를 찾을 수 없습니다")
            s.update(modality=form["modality"], modality_basis=f"웹에서 지정 ({form.get('by') or '검토자'})",
                     needs_review=False)
            errors = save_plan(run, plan, f"modality {form['modality']}: {s['series_key']} (웹)")
        except RunError as e:
            return back(run.id, err=str(e), anchor="unresolved")
        return result(run, errors, "modality를 저장했습니다", "unresolved")

    def set_pilot_subjects(run: Run, form: dict) -> list[str]:
        editable(run)
        plan = run.load("plan.json")
        active = {s["subject_key"] for s in plan["series"] if not s.get("excluded")}
        chosen = [form.get(f"pilot{i}", "") for i in range(1, SAMPLES + 1)]
        chosen = [c for i, c in enumerate(chosen) if c and c not in chosen[:i]]
        bad = [c for c in chosen if c not in active]
        if bad or not chosen:
            raise RunError(f"포함된 대상자가 아닙니다: {', '.join(bad) or '(비어 있음)'}")
        plan["pilot"] = {"subjects": chosen}
        return save_plan(run, plan, f"파일럿 대상자: {', '.join(chosen)} (웹)")

    @app.post("/runs/{run_id}/pilot-subjects")
    async def edit_pilot_subjects(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            errors = set_pilot_subjects(run, form)
        except RunError as e:
            return back(run.id, err=str(e), anchor="pilot")
        return result(run, errors, "파일럿 대상자를 저장했습니다", "pilot")

    def subjects_from_form(run: Run, form: dict) -> None:
        """Subjects typed beside the sample/send buttons are saved first when they differ from the plan."""
        chosen = [c for c in (form.get(f"pilot{i}") for i in range(1, SAMPLES + 1)) if c]
        if chosen and chosen != ((run.load("plan.json") or {}).get("pilot") or {}).get("subjects"):
            errors = set_pilot_subjects(run, form)
            if errors:
                raise RunError(f"plan 오류 {len(errors)}건: {errors[0]}")

    def check_pilot_state(run: Run) -> None:
        meta = run.meta
        if meta["status"] not in ("PLANNED", "PILOT_READY", "PILOT_SENT", "PILOT_PARTIAL", "PILOT_FAILED"):
            raise RunError(f"{meta['status']} 상태에서는 샘플을 만들거나 보낼 수 없습니다 (plan 오류를 먼저 해결)")
        if sha256_json(run.load("plan.json")) != meta["plan_sha256"]:
            raise RunError("plan.json이 검증 이후 바뀌었습니다")

    @app.post("/runs/{run_id}/sample")
    async def start_sample(request: Request, run_id: str):
        """Convert only the sample subjects so their real and outgoing values can be compared; sends nothing."""
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            subjects_from_form(run, form)
            check_pilot_state(run)
            submit(settings, "sample", run.id, {}, form.get("by", ""))
        except RunError as e:
            return back(run.id, err=str(e), anchor="samples")
        return back(run.id, msg="샘플을 추출합니다. 끝나면 이 화면에 원본 값과 입력될 값이 나옵니다.", anchor="samples")

    @app.post("/runs/{run_id}/pilot")
    async def start_pilot(request: Request, run_id: str):
        """Send the sample subjects to the local SCP (extracting them first when needed)."""
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            subjects_from_form(run, form)
            check_pilot_state(run)
            samples = ((run.load("plan.json") or {}).get("pilot") or {}).get("subjects") or []
            subject = form.get("subject") or None
            if subject is not None and subject not in samples:
                raise RunError(f"샘플이 아닙니다: {subject}")
            submit(settings, "pilot", run.id, {"target": settings.local_scp.key, "subject": subject},
                   form.get("by", ""))
        except RunError as e:
            return back(run.id, err=str(e), anchor="pilot")
        return back(run.id, msg=f"{subject or '샘플'} → Local SCP 전송을 시작했습니다 ({settings.local_scp.key})",
                    anchor="pilot")

    @app.post("/runs/{run_id}/deid")
    async def set_deid(request: Request, run_id: str):
        """Pseudonymisation on/off (off by default: public data are de-identified already). The samples are
        re-extracted so the page shows what would now leave."""
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            editable(run)
            plan = run.load("plan.json")
            on = form.get("on") == "1"
            (deid.enable if on else deid.disable)(plan)
            errors = save_plan(run, plan, f"비식별 {'켬' if on else '끔'} (웹)")
            if not errors and (plan.get("pilot") or {}).get("subjects"):
                submit(settings, "sample", run.id, {}, form.get("by", ""))
        except RunError as e:
            return back(run.id, err=str(e))
        return result(run, errors, f"비식별을 {'켰' if on else '껐'}습니다. 샘플을 다시 추출합니다.")

    @app.post("/runs/{run_id}/approve")
    async def do_approve(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            if active_job(settings, run.id):
                raise RunError("작업이 진행 중입니다")
            approve(run, form.get("approver", ""), form.get("confirm", ""), form.get("note", ""))
            ok, why = central_ready(settings.central)
            if ok:
                submit(settings, "publish", run.id, {"target": settings.central.key}, form.get("approver", ""))
                return back(run.id, msg="승인했습니다. 전체 변환 → Central 전송 → DB 등록을 시작합니다.", anchor="approve")
        except RunError as e:
            return back(run.id, err=str(e), anchor="approve")
        return back(run.id, msg=f"승인을 기록했습니다. Central 전송은 대기합니다: {why}", anchor="approve")

    @app.post("/runs/{run_id}/publish")
    async def start_publish(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            ok, why = central_ready(settings.central)
            if not ok:
                raise RunError(why)
            if not run.meta.get("approval"):
                raise RunError("승인 기록이 없습니다")
            submit(settings, "publish", run.id, {"target": settings.central.key}, form.get("by", ""))
        except RunError as e:
            return back(run.id, err=str(e), anchor="central")
        return back(run.id, msg=f"Central 전송을 시작했습니다 → {settings.central.key}", anchor="central")

    @app.post("/runs/{run_id}/reanalyze")
    async def reanalyze(request: Request, run_id: str):
        run = get_run(run_id)
        form = await checked_form(request)
        try:
            if run.meta["status"] not in ("ANALYSIS_FAILED", "PLANNED", "NEEDS_INPUT"):
                raise RunError("분석 실패 또는 검토 단계에서만 다시 분석할 수 있습니다 (파일럿 이후에는 새 분석)")
            submit(settings, "analyze", run.id, {"input_root": run.meta["input_root"]}, form.get("by", ""))
        except RunError as e:
            return back(run.id, err=str(e))
        return back(run.id, msg="다시 분석을 시작했습니다")
