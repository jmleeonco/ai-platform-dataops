"""데이터셋 — the one dataset list (CONTRACT §78.8, amending the §78.7 web): every registered dataset (old DB,
deployment bundles, this system's intake) and every catalogue entry on one page, one row per dataset, each with
where it stands; the §78.7 filters plus stand, registration and source, combinable in the URL with counts per value;
one-click "exclude non-commercial"; CSV / xlsx export of the filtered list; the entry page with every imported
field, its folders, its registered dataset links, hand edits and the intake action. `/catalog` redirects here.

Reads the catalogue and management tables; the POSTs (hand edit, confirm / reject a folder, link / unlink a
registered dataset) write the catalogue tables and nothing else. Nothing here downloads, moves, deletes or sends.
"""
from __future__ import annotations

import io
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import catalogue as cat
from .. import dataset_list as dl
from .. import db as dbmod
from .actions import checked_form
from .prefix import url

PARAMS = list(dl.FACETS)
LABELS = {"commercial": cat.COMMERCIAL_KO, "availability": {**cat.AVAILABILITY_KO, dl.NOT_IN_CATALOGUE: "목록 밖"},
          "origin": {**cat.ORIGIN_KO, "unknown": "모름"}, "new_rois": {"yes": "있음", "no": "없음"},
          "kind": {"list": "목록 행", "folder": "내부 폴더", "marker": "팀 메모 행", "registered": "등록만 (목록 밖)"},
          "stand": dl.STAND_KO, "registered": dl.REGISTERED_KO, "source": dl.SOURCES, "link": dl.LINKS,
          "method": dl.LINK_METHODS}
OPEN_FACETS = ("stand", "registered", "commercial", "source")


def _params(request: Request) -> tuple[dict[str, list[str]], str]:
    qp = request.query_params
    params = {k: [v for v in qp.getlist(k) if v][:50] for k in PARAMS if qp.getlist(k)}
    old = [v for v in params.get("origin", []) if v in dl.OLD_ORIGIN]      # the old page's ?origin=deploy
    if old:
        params["source"] = sorted(set(params.get("source", []) + old))
        params["origin"] = [v for v in params["origin"] if v not in dl.OLD_ORIGIN]
        if not params["origin"]:
            del params["origin"]
    return params, (qp.get("q") or "").strip()[:200]


def _query(params: dict, q: str, **change) -> str:
    """The URL query for the current filters with `change` applied (value None drops the facet)."""
    p = {k: list(v) for k, v in params.items()}
    for k, v in change.items():
        if v is None:
            p.pop(k, None)
        else:
            p[k] = v
    pairs = [(k, v) for k in PARAMS for v in p.get(k, [])]
    if q:
        pairs.append(("q", q))
    return urlencode(pairs)


def _in_input_root(path: str, roots) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def register_catalogue_views(app, settings, page, db_conn, csv_response, lists: dl.ListCache) -> None:
    def empty(request, err):
        return page(request, "datasets.html", db_error=err, rows=[], counts={}, params={}, q="", facets=dl.FACETS,
                    labels=LABELS, unmatched=[], total=0, qs="", query=_query, open_facets=(), totals=None)

    def with_conn(request, fn):
        conn, err = db_conn()
        if conn is None:
            return empty(request, err)
        try:
            return fn(conn)
        except dbmod.pymysql.MySQLError as e:
            return empty(request, f"데이터셋 표를 읽지 못했습니다 (db-migrate 필요?): {e}")
        finally:
            conn.close()

    @app.get("/datasets", response_class=HTMLResponse)
    def datasets_page(request: Request):
        params, q = _params(request)

        def run(conn):
            rows, entries, extra, _ = lists.get(conn)
            shown, counts = dl.apply_filters(rows, params, q)
            imports = dbmod.query(conn, "SELECT kind, file_name, rows_read, run_at FROM catalog_imports"
                                        " ORDER BY import_id DESC")
            last = {k: next((i for i in imports if i["kind"] == k), None) for k in ("list", "scan", "sizes")}
            return page(request, "datasets.html", rows=shown, counts=counts, params=params, q=q, facets=dl.FACETS,
                        labels=LABELS, total=len(rows), unmatched=extra["unmatched"], last=last,
                        qs=_query(params, q), query=_query, open_facets=OPEN_FACETS, totals=extra["totals"],
                        n_entries=len(entries), n_registered=len(extra["datasets"]),
                        tot={"stand": dict(cat.Counter(r["stand"] for r in rows)),
                             "registered": dict(cat.Counter(r["registered"] for r in rows)),
                             "commercial_use": dict(cat.Counter(r["eff"]["commercial_use"] for r in rows))})
        return with_conn(request, run)

    def filtered(request):
        params, q = _params(request)
        conn, err = db_conn()
        if conn is None:
            raise HTTPException(503, err)
        try:
            rows, *_ = lists.get(conn)
            headers = cat.last_headers(conn)
        finally:
            conn.close()
        shown, _ = dl.apply_filters(rows, params, q)
        return dl.export_rows(shown, headers)

    @app.get("/datasets.csv")
    def datasets_csv(request: Request):
        return csv_response(filtered(request), "datasets.csv")

    @app.get("/datasets.xlsx")
    def datasets_xlsx(request: Request):
        import openpyxl
        from openpyxl.utils import get_column_letter
        table = filtered(request)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "datasets"
        for r in table:
            ws.append([_xl(v) for v in r])
        ws.freeze_panes = "B2"
        for i in range(1, len(table[0]) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 18
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="datasets.xlsx"'})

    def moved(request: Request, to: str) -> RedirectResponse:
        q = request.url.query
        return RedirectResponse(url(to) + (f"?{q}" if q else ""), status_code=302)

    @app.get("/catalog")
    def catalog_moved(request: Request):
        return moved(request, "/datasets")

    @app.get("/catalog.csv")
    def catalog_csv_moved(request: Request):
        return moved(request, "/datasets.csv")

    @app.get("/catalog.xlsx")
    def catalog_xlsx_moved(request: Request):
        return moved(request, "/datasets.xlsx")

    @app.get("/catalog/{entry_id}", response_class=HTMLResponse)
    def entry_page(request: Request, entry_id: int):
        def run(conn):
            rows, entries, extra, datasets = lists.get(conn)
            e = next((x for x in entries if x["entry_id"] == entry_id), None)
            if e is None:
                raise HTTPException(404, "카탈로그 항목이 없습니다")
            row = next(r for r in rows if any(x["entry_id"] == entry_id for x in r["entries"]))
            headers = cat.last_headers(conn) or list(e["fields"])
            edits = dbmod.query(conn, "SELECT * FROM catalog_edits WHERE entry_id=%s ORDER BY edit_id DESC",
                                (entry_id,))
            sources = [m["folder"] for m in e["folders"] if m["state"] == "confirmed" and m["folder"]["present"]]
            intake = [f for f in sources if _in_input_root(f["path"], settings.input_roots)]
            by_uuid = {d["dataset_uuid"]: d for d in datasets}
            links = [dict(x, dataset=by_uuid[x["dataset_uuid"]]) for x in e.get("dataset_links", [])
                     if x["dataset_uuid"] in by_uuid]
            linked = {x["dataset_uuid"] for x in links}
            return page(request, "catalog_entry.html", e=e, row=row, headers=headers, edits=edits, labels=LABELS,
                        intake=intake, sources=sources, unmatched=extra["unmatched"], editable=cat.EDITABLE,
                        input_roots=settings.input_roots, quote=quote, links=links,
                        linkable=[d for d in datasets if d["dataset_uuid"] not in linked])
        return with_conn(request, run)

    def back(entry_id: int, msg: str = "", err: str = "") -> RedirectResponse:
        q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse(url(f"/catalog/{entry_id}{q}"), status_code=303)

    def write(entry_id: int, fn, ok: str):
        conn, err = db_conn()
        if conn is None:
            return back(entry_id, err=err)
        try:
            msg = fn(conn)
        except (cat.CatalogueError, dl.LinkError) as e:
            return back(entry_id, err=str(e))
        finally:
            conn.close()
        lists.clear()
        return back(entry_id, msg=msg or ok)

    @app.post("/catalog/{entry_id}/edit")
    async def entry_edit(request: Request, entry_id: int):
        form = await checked_form(request)

        def fn(conn):
            done = cat.edit_entry(conn, entry_id, {f: form.get(f, "") for f in cat.EDITABLE if f in form},
                                  form.get("note", ""), form.get("by", ""))
            return f"저장했습니다: {', '.join(done)}" if done else "바뀐 값이 없습니다"
        return write(entry_id, fn, "")

    @app.post("/catalog/{entry_id}/match")
    async def entry_match(request: Request, entry_id: int):
        form = await checked_form(request)

        def fn(conn):
            if not (form.get("by") or "").strip():
                raise cat.CatalogueError("누가 확인했는지 적어 주세요")
            try:
                folder_id = int(form.get("folder_id", ""))
            except ValueError:
                raise cat.CatalogueError("폴더를 고르세요") from None
            cat.decide_match(conn, entry_id, folder_id, form.get("state", ""), form["by"], form.get("note", ""))
        return write(entry_id, fn, "폴더 연결을 저장했습니다 (보유 상태를 다시 계산했습니다)")

    @app.post("/catalog/{entry_id}/dataset")
    async def entry_dataset(request: Request, entry_id: int):
        form = await checked_form(request)

        def fn(conn):
            dl.decide_link(conn, entry_id, form.get("dataset_uuid", ""), form.get("state", ""),
                           form.get("by", ""), form.get("note", ""))
        return write(entry_id, fn, "등록 데이터셋 연결을 저장했습니다")


def _xl(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    if s[:1] in ("=", "+", "-", "@"):
        s = "'" + s                                   # dataset text must not become a spreadsheet formula
    return s[:32000]
