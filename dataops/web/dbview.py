"""Read-only browser for the management DB: an overview of the tables and how they link, a table view with
search / exact filters / sorting / CSV, and a row view that follows foreign keys up (parent rows) and down
(child rows). Every page is live: it polls /db/state.json and re-renders itself when a table it shows
changes. Only SELECTs; table and column names come from information_schema and are whitelisted, values are
always bound parameters."""
from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import db as dbmod
from ..util import now_iso

PAGE_SIZES = (25, 50, 100, 200)
LIVE_SECONDS = 5
CSV_LIMIT = 100_000
RESERVED = {"q", "sort", "dir", "p", "n"}
TABLE_DESC = {
    "datasets": "공개 데이터셋 (이름·출처·라이선스·상업적 이용)",
    "subjects": "데이터셋별 대상자 (원본 키 → PatientID)",
    "studies": "대상자별 검사 (Study UID)",
    "series": "검사별 시리즈 (Modality·설명·메타데이터·인스턴스 수)",
    "dataset_series": "데이터셋과 시리즈의 소속 관계",
    "ingest_runs": "등록 실행 (plan·승인 기록·manifest)",
    "instances": "DICOM 파일 (SOP UID·산출물 경로·해시·원본 파일)",
    "transfer_results": "SOP별 전송 결과 (대상·상태·시도 횟수)",
    "external_mappings": "외부 시스템의 데이터셋 ID 매핑",
    "schema_migrations": "적용된 DB migration",
}
SOFT_FKS = {("series", "first_run_uuid"): ("ingest_runs", "run_uuid")}   # links without a DB constraint
CHAIN = ["datasets", "subjects", "studies", "series", "instances", "transfer_results"]
TEXTY = ("char", "varchar", "text", "tinytext", "mediumtext", "longtext", "json", "enum")


def schema(conn) -> dict:
    """{table: {"columns": [...], "types": {col: data_type}, "pk": [...], "fks": {col: (table, col)},
    "children": [(child_table, child_col, col)]}} for the browsable tables."""
    cols = dbmod.query(conn, "SELECT TABLE_NAME t, COLUMN_NAME c, DATA_TYPE d, COLUMN_TYPE ct, IS_NULLABLE n,"
                             " COLUMN_KEY k FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()"
                             " ORDER BY TABLE_NAME, ORDINAL_POSITION")
    fks = dbmod.query(conn, "SELECT TABLE_NAME t, COLUMN_NAME c, REFERENCED_TABLE_NAME rt, REFERENCED_COLUMN_NAME rc"
                            " FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE()"
                            " AND REFERENCED_TABLE_NAME IS NOT NULL")
    pks = dbmod.query(conn, "SELECT TABLE_NAME t, COLUMN_NAME c FROM information_schema.KEY_COLUMN_USAGE"
                            " WHERE TABLE_SCHEMA=DATABASE() AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION")
    out = {t: {"columns": [], "types": {}, "column_info": [], "pk": [], "fks": {}, "children": []}
           for t in dbmod.BROWSABLE}
    for r in cols:
        if r["t"] in out:
            out[r["t"]]["columns"].append(r["c"])
            out[r["t"]]["types"][r["c"]] = r["d"]
            out[r["t"]]["column_info"].append(r)
    for r in pks:
        if r["t"] in out:
            out[r["t"]]["pk"].append(r["c"])
    fks = fks + [{"t": t, "c": c, "rt": rt, "rc": rc} for (t, c), (rt, rc) in SOFT_FKS.items()]
    for r in fks:
        if r["t"] in out and r["rt"] in out and r["c"] in out[r["t"]]["types"]:
            out[r["t"]]["fks"][r["c"]] = (r["rt"], r["rc"])
            out[r["rt"]]["children"].append((r["t"], r["c"], r["rc"]))
    return {t: v for t, v in out.items() if v["columns"]}


def _where(meta: dict, params: dict, q: str) -> tuple[str, list, dict]:
    clauses, args, filters = [], [], {}
    for col, val in params.items():
        if col in meta["columns"] and col not in RESERVED:
            clauses.append(f"`{col}` <=> %s" if val != "NULL" else f"`{col}` IS NULL")
            if val != "NULL":
                args.append(val)
            filters[col] = val
    if q:
        texty = [c for c in meta["columns"] if meta["types"][c] in TEXTY]
        if texty:
            clauses.append("(" + " OR ".join(f"`{c}` LIKE %s" for c in texty) + ")")
            args += [f"%{q}%"] * len(texty)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args, filters


def fk_href(sch: dict, fk, value) -> str | None:
    """Row page of the referenced row when the reference is its whole primary key, else a filtered list."""
    if not fk or value is None:
        return None
    rt, rc = fk
    if sch.get(rt, {}).get("pk") == [rc]:
        return f"/db/{rt}/row?" + urlencode({rc: str(value)})
    return f"/db/{rt}?" + urlencode({rc: str(value)})


def live_sig(marks: dict, tables: list[str]) -> str:
    """What dblive.js compares: the change marks of the tables a page shows, in page order."""
    return ";".join(marks.get(t, "") for t in tables)


def _display(v, limit=160):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    s = str(v)
    return s if len(s) <= limit else s[:limit] + " …"


def _pretty(v):
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.dumps(json.loads(v), ensure_ascii=False, indent=2, sort_keys=True)
        except ValueError:
            pass
    return None if v is None else str(v)


def register_db_views(app, settings, page, db_conn, to_csv):
    def open_db():
        conn, err = db_conn()
        if conn is None:
            raise _NoDb(err)
        return conn

    @app.exception_handler(_NoDb)
    async def no_db(request: Request, exc: _NoDb):
        return page(request, "db.html", info=None, db_error=exc.message)

    def table_meta(conn, table) -> tuple[dict, dict]:
        sch = schema(conn)
        if table not in sch:
            raise HTTPException(404)
        return sch, sch[table]

    def live(conn, tables, exact=True) -> dict:
        return {"tables": ",".join(tables), "exact": int(exact), "every": LIVE_SECONDS, "at": now_iso(),
                "sig": live_sig(dbmod.change_marks(conn, tables, exact), tables)}

    @app.get("/db/state.json")
    def db_state(t: str = "", exact: int = 0):
        conn, err = db_conn()
        if conn is None:
            return JSONResponse({"error": err, "checked_at": now_iso()})
        try:
            sch = schema(conn)
            tables = [x for x in t.split(",") if x in sch] if t else list(sch)
            return {"marks": dbmod.change_marks(conn, tables, bool(exact)), "checked_at": now_iso()}
        except Exception as e:  # noqa: BLE001 - the page shows it and keeps polling
            return JSONResponse({"error": str(e)[:300], "checked_at": now_iso()})
        finally:
            conn.close()

    @app.get("/db", response_class=HTMLResponse)
    def db_overview(request: Request):
        conn = open_db()
        try:
            sch = schema(conn)
            info = {"migrations": dbmod.migration_status(conn), "counts": dbmod.table_counts(conn), "schema": sch}
            lv = live(conn, list(sch), exact=False)          # the overview shows row counts only
        finally:
            conn.close()
        return page(request, "db.html", info=info, db_error=None, desc=TABLE_DESC, live=lv,
                    chain=[t for t in CHAIN if t in sch], others=[t for t in sch if t not in CHAIN])

    def select(request, table, q, sort, dir, p, n, limit=None):
        if table not in dbmod.BROWSABLE:
            raise HTTPException(404)
        conn = open_db()
        try:
            sch, meta = table_meta(conn, table)
            where, args, filters = _where(meta, dict(request.query_params), q)
            order_col = sort if sort in meta["columns"] else (meta["pk"][0] if meta["pk"] else meta["columns"][0])
            order = f" ORDER BY `{order_col}` {'DESC' if dir == 'desc' else 'ASC'}"
            total = dbmod.query(conn, f"SELECT COUNT(*) AS n FROM `{table}`{where}", args)[0]["n"]
            lim = limit or n
            rows = dbmod.query(conn, f"SELECT * FROM `{table}`{where}{order} LIMIT {int(lim)} OFFSET %s",
                               [*args, 0 if limit else (p - 1) * n])
            lv = None if limit else live(conn, [table])
        finally:
            conn.close()
        return sch, meta, rows, total, filters, order_col, lv

    @app.get("/db/{table}.csv")
    def db_table_csv(request: Request, table: str, q: str = "", sort: str = "", dir: str = "asc"):
        sch, meta, rows, total, filters, _, _ = select(request, table, q, sort, dir, 1, 0, limit=CSV_LIMIT)
        return to_csv([meta["columns"]] + [[_display(r[c], 10**9) for c in meta["columns"]] for r in rows],
                      f"{table}.csv")

    @app.get("/db/{table}", response_class=HTMLResponse)
    def db_table(request: Request, table: str, q: str = "", sort: str = "", dir: str = "asc",
                 p: int = Query(1, ge=1), n: int = Query(50)):
        n = n if n in PAGE_SIZES else 50
        sch, meta, rows, total, filters, order_col, lv = select(request, table, q, sort, dir, p, n)
        base = {**filters, **({"q": q} if q else {})}

        def link(**kw):
            return "?" + urlencode({**base, "sort": order_col, "dir": dir, "n": n, **kw})
        cells = [[{"col": c, "text": _display(r[c]), "href": fk_href(sch, meta["fks"].get(c), r[c]),
                   "raw": None if r[c] is None else str(r[c])} for c in meta["columns"]] for r in rows]
        pk_links = [urlencode({k: str(r[k]) for k in meta["pk"]}) if meta["pk"] else None for r in rows]
        return page(request, "table.html", table=table, meta=meta, desc=TABLE_DESC.get(table, ""), cells=cells,
                    pk_links=pk_links, total=total, p=p, n=n, pages=max(1, -(-total // n)), q=q, filters=filters,
                    sort=order_col, dir=dir, link=link, page_sizes=PAGE_SIZES, live=lv,
                    csv_link=f"/db/{table}.csv?" + urlencode({**base, "sort": order_col, "dir": dir}))

    @app.get("/db/{table}/row", response_class=HTMLResponse)
    def db_row(request: Request, table: str):
        if table not in dbmod.BROWSABLE:
            raise HTTPException(404)
        conn = open_db()
        try:
            sch, meta = table_meta(conn, table)
            key = {k: request.query_params.get(k) for k in meta["pk"]}
            if not meta["pk"] or any(v is None for v in key.values()):
                raise HTTPException(404)
            rows = dbmod.query(conn, f"SELECT * FROM `{table}` WHERE " +
                               " AND ".join(f"`{k}`=%s" for k in key), list(key.values()))
            if not rows:
                raise HTTPException(404)
            row = rows[0]
            children = []
            for child, ccol, pcol in meta["children"]:
                cnt = dbmod.query(conn, f"SELECT COUNT(*) AS n FROM `{child}` WHERE `{ccol}`=%s", (row[pcol],))[0]["n"]
                children.append({"table": child, "col": ccol, "n": cnt,
                                 "href": f"/db/{child}?" + urlencode({ccol: str(row[pcol])})})
            lv = live(conn, [table] + sorted({c[0] for c in meta["children"]} - {table}))
        finally:
            conn.close()
        fields = [{"col": c, "type": meta["types"][c], "value": _pretty(row[c]),
                   "href": fk_href(sch, meta["fks"].get(c), row[c]), "fk": meta["fks"].get(c)}
                  for c in meta["columns"]]
        return page(request, "db_row.html", table=table, desc=TABLE_DESC.get(table, ""), key=key, fields=fields,
                    children=children, live=lv)


class _NoDb(Exception):
    def __init__(self, message):
        self.message = message
