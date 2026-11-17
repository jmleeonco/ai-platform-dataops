"""The dataset catalogue pages (CONTRACT §78.7): every known dataset with availability and commercial use as badges,
combinable filters kept in the URL with counts per value, one-click "exclude non-commercial", CSV / xlsx export of
the filtered list, and an entry page with every imported field, its folders, hand edits and the intake action.

Reads the catalogue tables only; the two POSTs (hand edit, confirm / reject a folder) write the catalogue tables
and nothing else. Nothing here downloads, moves or deletes a file.
"""
from __future__ import annotations

import io
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import catalogue as cat
from .. import db as dbmod
from .actions import checked_form
from .prefix import url

PARAMS = list(cat.FACETS)
LABELS = {"commercial": cat.COMMERCIAL_KO, "availability": cat.AVAILABILITY_KO, "origin": cat.ORIGIN_KO,
          "new_rois": {"yes": "있음", "no": "없음"}, "kind": {"list": "목록 행", "folder": "내부 폴더", "marker": "팀 메모 행"}}
OPEN_FACETS = ("commercial", "availability", "origin", "licence")


def _params(request: Request) -> tuple[dict[str, list[str]], str]:
    qp = request.query_params
    params = {k: [v for v in qp.getlist(k) if v][:50] for k in PARAMS if qp.getlist(k)}
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


def register_catalogue_views(app, settings, page, db_conn, csv_response) -> None:
    def conn_or_page(request, fn):
        conn, err = db_conn()
        if conn is None:
            return page(request, "catalog.html", db_error=err, rows=[], counts={}, params={}, q="", facets=cat.FACETS,
                        labels=LABELS, summary=None, unmatched=[], total=0, qs="", query=_query, open_facets=())
        try:
            return fn(conn)
        except dbmod.pymysql.MySQLError as e:
            return page(request, "catalog.html", db_error=f"카탈로그 표가 없습니다 (db-migrate 필요?): {e}", rows=[],
                        counts={}, params={}, q="", facets=cat.FACETS, labels=LABELS, summary=None, unmatched=[],
                        total=0, qs="", query=_query, open_facets=())
        finally:
            conn.close()

    @app.get("/catalog", response_class=HTMLResponse)
    def catalog_page(request: Request):
        params, q = _params(request)

        def run(conn):
            entries, extra = cat.load(conn)
            rows, counts = cat.apply_filters(entries, params, q)
            with conn.cursor() as cur:
                cur.execute("SELECT kind, file_name, rows_read, run_at FROM catalog_imports ORDER BY import_id DESC")
                imports = list(cur.fetchall())
            last = {k: next((i for i in imports if i["kind"] == k), None) for k in ("list", "scan", "sizes")}
            return page(request, "catalog.html", rows=rows, counts=counts, params=params, q=q, facets=cat.FACETS,
                        labels=LABELS, total=len(entries), unmatched=extra["unmatched"], last=last,
                        qs=_query(params, q), query=_query, open_facets=OPEN_FACETS,
                        tot={f: dict(cat.Counter(e["eff"][f] for e in entries))
                             for f in ("availability", "commercial_use")})
        return conn_or_page(request, run)

    def filtered(request):
        params, q = _params(request)
        conn, err = db_conn()
        if conn is None:
            raise HTTPException(503, err)
        try:
            entries, _ = cat.load(conn)
            headers = cat.last_headers(conn)
        finally:
            conn.close()
        rows, _ = cat.apply_filters(entries, params, q)
        return cat.export_rows(rows, headers)

    @app.get("/catalog.csv")
    def catalog_csv(request: Request):
        return csv_response(filtered(request), "dataset-catalogue.csv")

    @app.get("/catalog.xlsx")
    def catalog_xlsx(request: Request):
        import openpyxl
        from openpyxl.utils import get_column_letter
        table = filtered(request)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "catalogue"
        for r in table:
            ws.append([_xl(v) for v in r])
        ws.freeze_panes = "B2"
        for i in range(1, len(table[0]) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 18
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="dataset-catalogue.xlsx"'})

    @app.get("/catalog/{entry_id}", response_class=HTMLResponse)
    def entry_page(request: Request, entry_id: int):
        def run(conn):
            entries, extra = cat.load(conn)
            e = next((x for x in entries if x["entry_id"] == entry_id), None)
            if e is None:
                raise HTTPException(404, "카탈로그 항목이 없습니다")
            headers = cat.last_headers(conn) or list(e["fields"])
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM catalog_edits WHERE entry_id=%s ORDER BY edit_id DESC", (entry_id,))
                edits = list(cur.fetchall())
            sources = [m["folder"] for m in e["folders"] if m["state"] == "confirmed" and m["folder"]["present"]]
            intake = [f for f in sources if _in_input_root(f["path"], settings.input_roots)]
            return page(request, "catalog_entry.html", e=e, headers=headers, edits=edits, labels=LABELS,
                        intake=intake, sources=sources, unmatched=extra["unmatched"], editable=cat.EDITABLE,
                        input_roots=settings.input_roots, quote=quote)
        return conn_or_page(request, run)

    def back(entry_id: int, msg: str = "", err: str = "") -> RedirectResponse:
        q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse(url(f"/catalog/{entry_id}{q}"), status_code=303)

    @app.post("/catalog/{entry_id}/edit")
    async def entry_edit(request: Request, entry_id: int):
        form = await checked_form(request)
        conn, err = db_conn()
        if conn is None:
            return back(entry_id, err=err)
        try:
            done = cat.edit_entry(conn, entry_id, {f: form.get(f, "") for f in cat.EDITABLE if f in form},
                                  form.get("note", ""), form.get("by", ""))
        except cat.CatalogueError as e:
            return back(entry_id, err=str(e))
        finally:
            conn.close()
        return back(entry_id, msg=f"저장했습니다: {', '.join(done)}" if done else "바뀐 값이 없습니다")

    @app.post("/catalog/{entry_id}/match")
    async def entry_match(request: Request, entry_id: int):
        form = await checked_form(request)
        conn, err = db_conn()
        if conn is None:
            return back(entry_id, err=err)
        try:
            if not (form.get("by") or "").strip():
                raise cat.CatalogueError("누가 확인했는지 적어 주세요")
            try:
                folder_id = int(form.get("folder_id", ""))
            except ValueError:
                raise cat.CatalogueError("폴더를 고르세요") from None
            cat.decide_match(conn, entry_id, folder_id, form.get("state", ""), form["by"])
        except cat.CatalogueError as e:
            return back(entry_id, err=str(e))
        finally:
            conn.close()
        return back(entry_id, msg="폴더 연결을 저장했습니다 (보유 상태를 다시 계산했습니다)")


def _xl(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    if s[:1] in ("=", "+", "-", "@"):
        s = "'" + s                                   # dataset text must not become a spreadsheet formula
    return s[:32000]
