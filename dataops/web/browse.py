"""The management DB as one flat table: a row per series of a dataset (or per patient of a dataset), with the
dataset, patient, study and series values side by side. One search box over every text column, a filter per
column, sorting, a column picker, paging and CSV.

Read only. Column expressions are fixed here (never taken from the request); every value is a bound parameter.
Source values are shown as they are (see docs: the web shows real values on the source side).
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db as dbmod
from .catalog import ORIGIN_SQL, ORIGINS, SOURCE_KINDS, roi_list

PAGE_SIZES = (50, 100, 200, 500)
CSV_LIMIT = 200_000
VALUES_TTL = 300                    # seconds the choices of a dropdown filter are reused
COUNT_TTL = 60                      # seconds a row count is reused (rows change on imports only), so paging is cheaper
NUM_RE = re.compile(r"(>=|<=|>|<|=)?\s*(-?\d+(?:\.\d+)?)")
LEGACY_JOIN = ("LEFT JOIN external_mappings x ON x.dataset_uuid=d.dataset_uuid"
               " AND x.target_system='oncolabdb.lab_datasets'")


def col(key, label, expr, kind="text", show=True, values_sql=None, labels=None):
    """kind: text (contains / =exact / - empty), num (>, <, >=, <=, =), enum (a dropdown of values_sql)."""
    return {"key": key, "label": label, "expr": expr, "kind": kind, "show": show, "values_sql": values_sql,
            "labels": labels or {}}


VIEWS = {
    "series": {
        "label": "시리즈 단위",
        "hint": "데이터셋에 속한 시리즈마다 한 줄 (같은 시리즈가 여러 데이터셋에 있으면 데이터셋마다 한 줄)",
        "from": f"""dataset_series ds JOIN datasets d ON d.dataset_uuid=ds.dataset_uuid {LEGACY_JOIN}
                    JOIN series se ON se.series_uid=ds.series_uid JOIN studies st ON st.study_uid=se.study_uid
                    JOIN subjects sb ON sb.subject_uuid=st.subject_uuid""",
        "extra": "d.dataset_uuid AS _dataset_uuid, se.series_uid AS _series_uid",
        "tiebreak": "sb.patient_id, st.study_date, se.series_number, ds.dataset_uuid, ds.series_uid",
        "columns": [
            col("dataset", "데이터셋", "d.name"),
            col("origin", "출처", ORIGIN_SQL, "enum", labels=ORIGINS),
            col("split", "구분", "ds.split", "enum", values_sql="SELECT DISTINCT split v FROM dataset_series"),
            col("patient_id", "PatientID", "sb.patient_id"),
            col("patient_name", "환자 이름", "sb.patient_name"),
            col("sex", "성별", "sb.patient_sex", "enum", False, "SELECT DISTINCT patient_sex v FROM subjects"),
            col("age", "나이", "sb.patient_age", show=False),
            col("study_date", "Study 날짜", "st.study_date"),
            col("study_desc", "Study 설명", "st.description", show=False),
            col("modality", "Modality", "se.modality", "enum", values_sql="SELECT DISTINCT modality v FROM series"),
            col("series_desc", "시리즈 설명", "se.description"),
            col("series_number", "번호", "se.series_number", "num", False),
            col("series_date", "시리즈 날짜", "se.series_date", show=False),
            col("files", "파일", "se.instance_count", "num"),
            col("rois", "ROI", "se.roi_names"),
            col("source_kind", "가져온 곳", "se.source_kind", "enum",
                values_sql="SELECT DISTINCT source_kind v FROM series", labels=SOURCE_KINDS),
            col("body_part", "부위", "se.body_part_examined", show=False),
            col("manufacturer", "제조사", "se.manufacturer", show=False),
            col("thickness", "두께", "se.slice_thickness", "num", False),
            col("pixel_spacing", "Pixel spacing", "se.pixel_spacing", show=False),
            col("series_uid", "Series UID", "se.series_uid"),
            col("study_uid", "Study UID", "st.study_uid", show=False),
            col("ref_series", "참조 영상 UID", "se.referenced_series_uid", show=False),
        ],
    },
    "patients": {
        "label": "환자 단위",
        "hint": "데이터셋에 속한 환자마다 한 줄 (데이터셋이 직접 등록한 환자 + 연결된 시리즈의 환자)",
        "from": f"""(SELECT dataset_uuid, subject_uuid, MAX(own) own, SUM(n) n_series,
                            GROUP_CONCAT(DISTINCT modality ORDER BY modality SEPARATOR ', ') modalities,
                            GROUP_CONCAT(DISTINCT split ORDER BY split SEPARATOR ' / ') splits
                     FROM (SELECT dataset_uuid, subject_uuid, 1 own, 0 n, NULL modality, NULL split FROM subjects
                           UNION ALL SELECT ds.dataset_uuid, st.subject_uuid, 0, 1, se.modality, ds.split
                           FROM dataset_series ds JOIN series se ON se.series_uid=ds.series_uid
                           JOIN studies st ON st.study_uid=se.study_uid) u
                     GROUP BY dataset_uuid, subject_uuid) m
                    JOIN datasets d ON d.dataset_uuid=m.dataset_uuid {LEGACY_JOIN}
                    JOIN subjects sb ON sb.subject_uuid=m.subject_uuid""",
        "extra": "d.dataset_uuid AS _dataset_uuid, NULL AS _series_uid",
        "tiebreak": "sb.patient_id, m.dataset_uuid, m.subject_uuid",
        "columns": [
            col("dataset", "데이터셋", "d.name"),
            col("origin", "출처", ORIGIN_SQL, "enum", labels=ORIGINS),
            col("patient_id", "PatientID", "sb.patient_id"),
            col("patient_name", "환자 이름", "sb.patient_name"),
            col("sex", "성별", "sb.patient_sex", "enum", values_sql="SELECT DISTINCT patient_sex v FROM subjects"),
            col("age", "나이", "sb.patient_age"),
            col("weight", "체중", "sb.patient_weight", show=False),
            col("source_key", "원본 키", "sb.source_subject_key", show=False),
            col("own", "소속", "m.own", "enum", labels={"1": "직접 등록", "0": "시리즈 연결"}),
            col("n_series", "시리즈", "m.n_series", "num"),
            col("modalities", "Modality", "m.modalities"),
            col("splits", "구분", "m.splits"),
        ],
    },
}


def _filter(c: dict, v: str) -> tuple[str | None, list]:
    e = c["expr"]
    if v == "-":
        return f"({e} IS NULL OR {e}='')", []
    if c["kind"] == "enum":
        return f"{e}=%s", [v]
    if c["kind"] == "num":
        m = NUM_RE.fullmatch(v.strip())
        return (f"{e}{m.group(1) or '='}%s", [m.group(2)]) if m else (None, [])
    if v.startswith("="):
        return f"{e}=%s", [v[1:]]
    if v.startswith("!"):
        return f"({e} IS NULL OR {e} NOT LIKE %s)", [f"%{v[1:]}%"]
    return f"{e} LIKE %s", [f"%{v}%"]


def build_where(view: dict, q: str, filters: dict) -> tuple[str, list, list[str]]:
    """WHERE over the view: every word of q must be in some text column; each column filter must hold."""
    clauses, args, bad = [], [], []
    texts = [c["expr"] for c in view["columns"] if c["kind"] == "text"]
    for word in q.split():
        clauses.append("(" + " OR ".join(f"{e} LIKE %s" for e in texts) + ")")
        args += [f"%{word}%"] * len(texts)
    by_key = {c["key"]: c for c in view["columns"]}
    for k, v in filters.items():
        sql, a = _filter(by_key[k], v)
        if sql is None:
            bad.append(k)
            continue
        clauses.append(sql)
        args += a
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args, bad


def _cell(c: dict, r: dict) -> dict:
    v = r[c["key"]]
    if v is None or v == "":
        return {"text": None}
    if c["key"] == "rois":
        names = roi_list(v)
        text = ", ".join(names)
        return {"text": text if len(text) <= 120 else text[:120] + " …", "title": f"{len(names)}개: {text}"}
    text = c["labels"].get(str(v), str(v))
    out = {"text": text}
    if c["key"] == "dataset":
        out["href"] = f"/datasets/{r['_dataset_uuid']}"
    elif c["key"] in ("series_uid", "series_desc") and r.get("_series_uid"):
        out["href"] = f"/series/{r['_series_uid']}"
    elif c["key"] == "ref_series":
        out["href"] = f"/series/{v}"
    elif c["key"] == "patient_id":
        out["href"] = "/search?q=" + quote(str(v))
    return out


def register_browse_views(app, page, db_conn, to_csv) -> None:
    choices, counts = {}, {}

    def values(conn, c) -> list:
        if c["labels"] and not c["values_sql"]:
            return list(c["labels"])
        hit = choices.get(c["values_sql"])
        if not hit or time.monotonic() - hit[0] > VALUES_TTL:
            got = sorted(str(r["v"]) for r in dbmod.query(conn, c["values_sql"]) if r["v"] not in (None, ""))
            hit = choices[c["values_sql"]] = (time.monotonic(), got)
        return hit[1]

    def count(conn, view, where, args) -> int:
        key = (view["label"], where, tuple(args))
        hit = counts.get(key)
        if not hit or time.monotonic() - hit[0] > COUNT_TTL:
            if len(counts) > 500:
                counts.clear()
            n = dbmod.query(conn, f"SELECT COUNT(*) AS n FROM {view['from']}{where}", args)[0]["n"]
            hit = counts[key] = (time.monotonic(), n)
        return hit[1]

    def parse(request: Request):
        qp = request.query_params
        vname = qp.get("view", "series")
        if vname not in VIEWS:
            raise HTTPException(404)
        view = VIEWS[vname]
        keys = [c["key"] for c in view["columns"]]
        shown = [k for k in qp.getlist("c") if k in keys] or [c["key"] for c in view["columns"] if c["show"]]
        filters = {k: qp[f"f_{k}"].strip() for k in keys if qp.get(f"f_{k}", "").strip()}
        sort = qp.get("sort") if qp.get("sort") in keys else "dataset"
        desc = qp.get("dir") == "desc"
        return vname, view, shown, filters, qp.get("q", "").strip(), sort, desc

    def run(conn, view, shown, filters, q, sort, desc, limit, offset):
        cols = [c for c in view["columns"] if c["key"] in shown]
        where, args, bad = build_where(view, q, filters)
        order = {c["key"]: c["expr"] for c in view["columns"]}[sort]
        sel = ", ".join(f"{c['expr']} AS `{c['key']}`" for c in cols)
        rows = dbmod.query(conn, f"SELECT {sel}, {view['extra']} FROM {view['from']}{where}"
                                 f" ORDER BY {order} {'DESC' if desc else 'ASC'}, {view['tiebreak']}"
                                 f" LIMIT {int(limit)} OFFSET {int(offset)}", args)
        return cols, rows, where, args, bad

    @app.get("/browse", response_class=HTMLResponse)
    def browse(request: Request):
        vname, view, shown, filters, q, sort, desc = parse(request)
        try:
            n = int(request.query_params.get("n", 100))
            p = max(1, int(request.query_params.get("p", 1)))
        except ValueError:
            n, p = 100, 1
        n = n if n in PAGE_SIZES else 100
        conn, err = db_conn()
        ctx = {"views": VIEWS, "vname": vname, "view": view, "shown": shown, "filters": filters, "q": q,
               "sort": sort, "desc": desc, "n": n, "p": p, "page_sizes": PAGE_SIZES}
        if conn is None:
            return page(request, "browse.html", db_error=err, cols=[], cells=[], total=0, pages=1, bad=[],
                        choices={}, elapsed=0, link=lambda **kw: "", csv_link="", **ctx)
        t0 = time.monotonic()
        try:
            cols, rows, where, args, bad = run(conn, view, shown, filters, q, sort, desc, n, (p - 1) * n)
            total = count(conn, view, where, args)
            opts = {c["key"]: values(conn, c) for c in view["columns"] if c["kind"] == "enum"}
        finally:
            conn.close()
        state = [("view", vname), *[("c", k) for k in shown], *[(f"f_{k}", v) for k, v in filters.items()],
                 *([("q", q)] if q else [])]

        def link(**kw):
            kw = {"sort": sort, "dir": "desc" if desc else "asc", "n": n, **kw}
            return "/browse?" + urlencode(state + list(kw.items()))
        cells = [[_cell(c, r) for c in cols] for r in rows]
        return page(request, "browse.html", db_error=None, cols=cols, cells=cells, total=total,
                    pages=max(1, -(-total // n)), bad=bad, choices=opts, elapsed=time.monotonic() - t0,
                    link=link, csv_link="/browse.csv?" + urlencode(state + [("sort", sort),
                                                                            ("dir", "desc" if desc else "asc")]),
                    **ctx)

    @app.get("/browse.csv")
    def browse_csv(request: Request):
        vname, view, shown, filters, q, sort, desc = parse(request)
        conn, err = db_conn()
        if conn is None:
            raise HTTPException(503, err)
        try:
            cols, rows, *_ = run(conn, view, shown, filters, q, sort, desc, CSV_LIMIT, 0)
        finally:
            conn.close()
        out = [[c["label"] for c in cols]]
        for r in rows:
            out.append([", ".join(roi_list(r[c["key"]])) if c["key"] == "rois" else
                        (None if r[c["key"]] is None else c["labels"].get(str(r[c["key"]]), r[c["key"]]))
                        for c in cols])
        return to_csv(out, f"management-db-{vname}.csv")
