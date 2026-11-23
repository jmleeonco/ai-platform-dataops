"""The 데이터셋 page's buttons (CONTRACT §102): 파일럿 보내기, 로컬 SCP로 보내기, 샘플 보기 for one row of the list.

GET  /datasets/row/{key}/send/{pilot|local}   confirmation: destination, number of subjects, what happens
POST /datasets/row/{key}/send/{pilot|local}   checks everything again, submits the existing job (admin only §79.2 d)
GET  /datasets/row/{key}/samples              at most 3 subjects as small PNGs (rendered on demand, cached)
GET  /datasets/row/{key}/samples/{png}        one PNG of that cache
POST /datasets/row/{key}/samples/refresh      drop this dataset's previews and render them again (admin only)

A button that cannot run says why and what comes first; nothing here starts an analysis. Never Central.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import db as dbmod
from .. import preview as pv
from .. import row_actions as ra
from ..jobs import submit
from ..retention import check_slot
from ..runs import Run, RunError
from .actions import checked_form
from .prefix import url

KEY_RE = re.compile(r"e\d{1,9}|d[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PNG_RE = re.compile(r"s\d_\d_z\d{1,5}\.png")


def context(settings, request: Request) -> ra.Context:
    return ra.Context(settings, getattr(request.state, "hub", None))


def register_dataset_actions(app, settings, page, db_conn, lists, labels) -> None:
    def row_of(key: str) -> dict:
        if not KEY_RE.fullmatch(key):
            raise HTTPException(404)
        conn, err = db_conn()
        if conn is None:
            raise HTTPException(503, err)
        try:
            rows, *_ = lists.get(conn)
        except dbmod.pymysql.MySQLError as e:
            raise HTTPException(503, str(e)) from None
        finally:
            conn.close()
        row = next((r for r in rows if r["key"] == key), None)
        if row is None:
            raise HTTPException(404, "목록에 없는 행입니다 (목록이 바뀌었으면 데이터셋에서 다시 여세요)")
        return row

    def back(key: str, msg: str = "", err: str = "", to: str | None = None) -> RedirectResponse:
        q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse(url((to or f"/datasets/row/{key}/samples") + q), status_code=303)

    def home(row: dict) -> str:
        if row["entries"]:
            return f"/catalog/{row['entry_id']}"
        return f"/datasets/{row['datasets'][0]['dataset_uuid']}"

    # ------------------------------------------------------------------ sends
    @app.get("/datasets/row/{key}/send/{what}", response_class=HTMLResponse)
    def send_confirm(request: Request, key: str, what: str):
        if what not in ("pilot", "local"):
            raise HTTPException(404)
        row = row_of(key)
        acts = ra.actions(row, context(settings, request), resolve_root=False)
        internal = ra.is_internal(row, settings, None)
        return page(request, "send_confirm.html", row=row, key=key, what=what, a=acts[what], acts=acts,
                    title=ra.WHAT[what], internal=internal, home=home(row), labels=labels)

    @app.post("/datasets/row/{key}/send/{what}")
    async def send_do(request: Request, key: str, what: str):
        if what not in ("pilot", "local"):
            raise HTTPException(404)
        form = await checked_form(request)
        row = row_of(key)
        a = ra.actions(row, context(settings, request), resolve_root=False)[what]
        to = f"/datasets/row/{key}/send/{what}"
        if form.get("confirm") != "yes":
            return back(key, err="확인 없이는 보내지 않습니다", to=to)
        if not a["ok"]:
            return back(key, err=a["why"], to=to)
        if form.get("run_id") != a["run_id"] or str(form.get("subjects")) != str(a["subjects"]):
            return back(key, err="확인한 뒤 상태가 바뀌었습니다 — 다시 확인하세요", to=to)
        run = Run(settings.runs_dir, a["run_id"])
        try:
            check_slot(run)
            if what == "pilot":
                submit(settings, "pilot", run.id, {"target": settings.local_scp.key, "subject": None},
                       form.get("by", ""))
            else:
                submit(settings, "local", run.id, {"target": settings.local_scp.key}, form.get("by", ""))
        except RunError as e:
            return back(key, err=str(e), to=to)
        lists.clear()
        msg = (f"파일럿 {a['subjects']}명 → {a['dest']} 전송을 시작했습니다 (Central 아님)" if what == "pilot" else
               f"전체 {a['subjects']}명 → {a['dest']} 로컬 전송을 시작했습니다 (Central 아님)")
        return RedirectResponse(url(f"/runs/{run.id}?msg={quote(msg)}"), status_code=303)

    # ------------------------------------------------------------------ samples
    def sample_state(request: Request, key: str):
        row = row_of(key)
        acts = ra.actions(row, context(settings, request), resolve_root=True)
        a = acts["samples"]
        internal = ra.is_internal(row, settings, a.get("root"))
        return row, acts, a, internal

    @app.get("/datasets/row/{key}/samples", response_class=HTMLResponse)
    def samples(request: Request, key: str):
        row, acts, a, internal = sample_state(request, key)
        meta = None
        if a["ok"]:
            pv.start(settings.preview_dir, a["root"], internal, settings.preview_cap_bytes)
            meta = pv.status(settings.preview_dir, a["root"]) or {"state": "rendering"}
        return page(request, "samples.html", row=row, key=key, a=a, acts=acts, meta=meta, internal=internal,
                    home=home(row), labels=labels, max_subjects=pv.MAX_SUBJECTS)

    @app.get("/datasets/row/{key}/samples/{name}")
    def sample_png(request: Request, key: str, name: str):
        if not PNG_RE.fullmatch(name):
            raise HTTPException(404)
        row, acts, a, internal = sample_state(request, key)
        if not a["ok"]:
            raise HTTPException(404)
        path = pv.cache_dir(settings.preview_dir, a["root"]) / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})

    @app.post("/datasets/row/{key}/samples/refresh")
    async def samples_refresh(request: Request, key: str):
        await checked_form(request)
        row, acts, a, internal = sample_state(request, key)
        if not a["ok"]:
            return back(key, err=a["why"])
        pv.purge(settings.preview_dir, a["root"])
        pv.start(settings.preview_dir, a["root"], internal, settings.preview_cap_bytes)
        return back(key, msg="샘플을 다시 만듭니다")
