"""Read-only browser for the management DB: an overview of the tables and how they link, a table view with
search / exact filters / sorting / CSV, and a row view that follows foreign keys up (parent rows) and down
(child rows). Every page is live: it polls /db/state.json and re-renders itself when a table it shows
changes. Only SELECTs; table and column names come from information_schema and are whitelisted, values are
always bound parameters."""
from __future__ import annotations

import json
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db as dbmod
from .. import legacy_db
from .actions import checked_form
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
    "deid_runs": "가명화한 run (가명화 방식·접두어) — 가명 대응 DB",
    "deid_subject_mappings": "원본 PatientID ↔ 가명 PatientID — 가명 대응 DB",
    "deid_series_mappings": "원본 Series/Study UID·경로 ↔ 보낸 Series UID — 가명 대응 DB",
    "deid_instance_mappings": "원본 SOP UID·파일 경로 ↔ 보낸 SOP UID — 가명 대응 DB",
}
SOFT_FKS = {("series", "first_run_uuid"): ("ingest_runs", "run_uuid")}   # links without a DB constraint
CHAIN = ["datasets", "subjects", "studies", "series", "instances", "transfer_results"]
TEXTY = ("char", "varchar", "text", "tinytext", "mediumtext", "longtext", "json", "enum")


def schema(conn, browsable: list[str] = dbmod.BROWSABLE) -> dict:
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
           for t in browsable}
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


def register_db_views(app, settings, page, db_conn, to_csv, map_conn=None):
    """map_conn(): (connection, error) of the pseudonymisation map DB; its tables (dbmod.DEID_TABLES) are browsed
    with the same pages, read from that DB."""
    def open_db():
        conn, err = db_conn()
        if conn is None:
            raise _NoDb(err)
        return conn

    def is_map(table) -> bool:
        return table in dbmod.DEID_TABLES

    def open_for(table):
        if not is_map(table):
            return open_db()
        conn, err = map_conn() if map_conn else (None, "가명 대응 DB 설정 없음")
        if conn is None:
            raise _NoDb(err)
        return conn

    def browsable(table) -> list[str]:
        return dbmod.DEIDMAP_BROWSABLE if is_map(table) else dbmod.BROWSABLE

    @app.exception_handler(_NoDb)
    async def no_db(request: Request, exc: _NoDb):
        return page(request, "db.html", info=None, db_error=exc.message)

    def table_meta(conn, table) -> tuple[dict, dict]:
        sch = schema(conn, browsable(table))
        if table not in sch:
            raise HTTPException(404)
        return sch, sch[table]

    def live(conn, tables, exact=True) -> dict:
        return {"tables": ",".join(tables), "exact": int(exact), "every": LIVE_SECONDS, "at": now_iso(),
                "sig": live_sig(dbmod.change_marks(conn, tables, exact), tables)}

    @app.get("/legacy", response_class=HTMLResponse)
    def legacy_page(request: Request):
        snap = legacy_db.load(settings.runtime_dir)
        new_types, new_db, conn, err = {}, None, *db_conn()
        if conn is not None:
            try:
                sch = schema(conn)
                new_types = {f"{t}.{c['c']}": c["ct"] for t, meta in sch.items() for c in meta["column_info"]}
                new_db = {"migrations": dbmod.migration_status(conn), "counts": dbmod.table_counts(conn)}
            finally:
                conn.close()
        return page(request, "legacy.html", snap=snap, v=legacy_db.view(snap, new_types), known=legacy_db.KNOWN,
                    configured=legacy_db.configured(settings.legacy_db), cfg=settings.legacy_db,
                    new_db=new_db, new_db_error=err)

    @app.post("/legacy/refresh")
    async def legacy_refresh(request: Request):
        await checked_form(request)
        try:
            snap = legacy_db.refresh(settings.legacy_db, settings.runtime_dir)
        except legacy_db.LegacyError as e:
            return RedirectResponse(f"/legacy?err={quote(str(e))}", status_code=303)
        msg = f"{len(snap['tables'])}개 테이블 구조를 읽었습니다 ({snap['host']}, {snap['version']})"
        return RedirectResponse(f"/legacy?msg={quote(msg)}", status_code=303)

    @app.get("/db/state.json")
    def db_state(t: str = "", exact: int = 0):
        asked = [x for x in t.split(",") if x]
        marks = {}
        for in_map in (False, True):
            if in_map and not (map_conn and (not asked or any(is_map(x) for x in asked))):
                continue
            if not in_map and asked and all(is_map(x) for x in asked):
                continue
            conn, err = map_conn() if in_map else db_conn()
            if conn is None:
                if in_map and not asked:
                    continue
                return JSONResponse({"error": err, "checked_at": now_iso()})
            try:
                sch = schema(conn, dbmod.DEIDMAP_BROWSABLE if in_map else dbmod.BROWSABLE)
                tables = [x for x in asked if x in sch and is_map(x) == in_map] if asked else \
                    [x for x in sch if is_map(x) == in_map]
                marks.update(dbmod.change_marks(conn, tables, bool(exact)))
            except Exception as e:  # noqa: BLE001 - the page shows it and keeps polling
                return JSONResponse({"error": str(e)[:300], "checked_at": now_iso()})
            finally:
                conn.close()
        return {"marks": marks, "checked_at": now_iso()}

    @app.get("/db", response_class=HTMLResponse)
    def db_overview(request: Request):
        conn = open_db()
        try:
            sch = schema(conn)
            info = {"migrations": dbmod.migration_status(conn), "counts": dbmod.table_counts(conn), "schema": sch}
            lv = live(conn, list(sch), exact=False)          # the overview shows row counts only
        finally:
            conn.close()
        deid_map, deid_map_error = None, None
        mconn, deid_map_error = map_conn() if map_conn else (None, "가명 대응 DB 설정 없음")
        if mconn is not None:
            try:
                d = dbmod.DEIDMAP_MIGRATIONS_DIR
                deid_map = {"migrations": dbmod.migration_status(mconn, d),
                            "counts": dbmod.table_counts(mconn, dbmod.DEID_TABLES), "tables": dbmod.DEID_TABLES}
            except Exception as e:  # noqa: BLE001 - shown on the page
                deid_map_error = str(e)[:300]
            finally:
                mconn.close()
        return page(request, "db.html", info=info, db_error=None, desc=TABLE_DESC, live=lv,
                    deid_map=deid_map, deid_map_error=deid_map_error,
                    chain=[t for t in CHAIN if t in sch], others=[t for t in sch if t not in CHAIN])

    def select(request, table, q, sort, dir, p, n, limit=None):
        if table not in browsable(table):
            raise HTTPException(404)
        conn = open_for(table)
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
        if table not in browsable(table):
            raise HTTPException(404)
        conn = open_for(table)
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
