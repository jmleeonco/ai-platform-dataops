"""The management DB as one flat table: a row per series of a dataset (or per patient of a dataset), with the
dataset, patient, study and series values side by side. A search panel (a word search over every text column,
dataset name and ROI name with suggestions, ticks for modality / split / origin …, a study date range and a
field for every other column), sorting, a column picker, paging and CSV.

Read only. Column expressions are fixed here (never taken from the request); every value is a bound parameter.
Source values are shown as they are (see docs: the web shows real values on the source side).
"""
from __future__ import annotations

import collections
import json
import re
import time
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db as dbmod
from .prefix import url
from .catalog import ORIGIN_SQL, ORIGINS, SOURCE_KINDS, roi_list

PAGE_SIZES = (50, 100, 200, 500)
CSV_LIMIT = 200_000
VALUES_TTL = 300                    # seconds the choices / suggestions of the search panel are reused
COUNT_TTL = 60                      # seconds a row count is reused (rows change on imports only), so paging is cheaper
ROI_SUGGEST = 2000                  # most used ROI names offered as suggestions
NUM_RE = re.compile(r"(>=|<=|>|<|=)?\s*(-?\d+(?:\.\d+)?)")
LEGACY_JOIN = ("LEFT JOIN external_mappings x ON x.dataset_uuid=d.dataset_uuid"
               " AND x.target_system='oncolabdb.lab_datasets'")
SPECIAL = ("roi", "roi_exact", "date_from", "date_to")      # panel fields that are not a column


def col(key, label, expr, kind="text", show=True, values_sql=None, labels=None, main=False):
    """kind: text (contains, a|b either, =exact, !exclude, - empty), num (>, <, >=, <=, =), enum (ticks of
    values_sql / labels), linked (shown only). main: in the always-open part of the search panel."""
    return {"key": key, "label": label, "expr": expr, "kind": kind, "show": show, "values_sql": values_sql,
            "labels": labels or {}, "main": main}


VIEWS = {
    "series": {
        "label": "시리즈 단위",
        "hint": "데이터셋에 속한 시리즈마다 한 줄 (같은 시리즈가 여러 데이터셋에 있으면 데이터셋마다 한 줄)",
        "from": f"""dataset_series ds JOIN datasets d ON d.dataset_uuid=ds.dataset_uuid {LEGACY_JOIN}
                    JOIN series se ON se.series_uid=ds.series_uid JOIN studies st ON st.study_uid=se.study_uid
                    JOIN subjects sb ON sb.subject_uuid=st.subject_uuid""",
        "extra": "d.dataset_uuid AS _dataset_uuid, se.series_uid AS _series_uid",
        "tiebreak": "sb.patient_id, st.study_date, se.series_number, ds.dataset_uuid, ds.series_uid",
        # an RTSTRUCT holding the ROI, or an image such an RTSTRUCT points at
        "roi_sql": "(se.roi_names LIKE %s OR EXISTS (SELECT 1 FROM series r"
                   " WHERE r.referenced_series_uid=se.series_uid AND r.roi_names LIKE %s))",
        "roi_args": 2,
        "date": "st.study_date",
        "columns": [
            col("dataset", "데이터셋", "d.name", main=True),
            col("origin", "출처", ORIGIN_SQL, "enum", labels=ORIGINS, main=True),
            col("split", "구분", "ds.split", "enum", values_sql="SELECT DISTINCT split v FROM dataset_series",
                main=True),
            col("patient_id", "PatientID", "sb.patient_id", main=True),
            col("patient_name", "환자 이름", "sb.patient_name", main=True),
            col("sex", "성별", "sb.patient_sex", "enum", False, "SELECT DISTINCT patient_sex v FROM subjects"),
            col("age", "나이", "sb.patient_age", show=False),
            col("study_date", "Study 날짜", "st.study_date"),
            col("study_desc", "Study 설명", "st.description", show=False),
            col("modality", "Modality", "se.modality", "enum", values_sql="SELECT DISTINCT modality v FROM series",
                main=True),
            col("series_desc", "시리즈 설명", "se.description", main=True),
            col("series_number", "번호", "se.series_number", "num", False),
            col("series_date", "시리즈 날짜", "se.series_date", show=False),
            col("files", "파일", "se.instance_count", "num"),
            col("rois", "ROI", "se.roi_names"),
            col("linked_rois", "연결된 ROI", "(SELECT GROUP_CONCAT(r.roi_names SEPARATOR ',') FROM series r"
                                           " WHERE r.referenced_series_uid=se.series_uid)", "linked"),
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
        # the patient has, in this dataset, an RTSTRUCT holding the ROI
        "roi_sql": "EXISTS (SELECT 1 FROM dataset_series ds2 JOIN series r ON r.series_uid=ds2.series_uid"
                   " JOIN studies s2 ON s2.study_uid=r.study_uid WHERE ds2.dataset_uuid=m.dataset_uuid"
                   " AND s2.subject_uuid=m.subject_uuid AND r.roi_names LIKE %s)",
        "roi_args": 1,
        "date": None,
        "columns": [
            col("dataset", "데이터셋", "d.name", main=True),
            col("origin", "출처", ORIGIN_SQL, "enum", labels=ORIGINS, main=True),
            col("patient_id", "PatientID", "sb.patient_id", main=True),
            col("patient_name", "환자 이름", "sb.patient_name", main=True),
            col("sex", "성별", "sb.patient_sex", "enum", values_sql="SELECT DISTINCT patient_sex v FROM subjects",
                main=True),
            col("age", "나이", "sb.patient_age"),
            col("weight", "체중", "sb.patient_weight", show=False),
            col("source_key", "원본 키", "sb.source_subject_key", show=False),
            col("own", "소속", "m.own", "enum", labels={"1": "직접 등록", "0": "시리즈 연결"}),
            col("n_series", "시리즈", "m.n_series", "num", main=True),
            col("modalities", "Modality", "m.modalities", main=True),
            col("splits", "구분", "m.splits"),
        ],
    },
}


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _text_filter(e: str, v: str) -> tuple[str, list]:
    if v == "-":
        return f"({e} IS NULL OR {e}='')", []
    if v.startswith("!"):
        return f"({e} IS NULL OR {e} NOT LIKE %s)", [f"%{v[1:]}%"]
    parts, args = [], []
    for alt in (a.strip() for a in v.split("|")):
        if alt:
            parts.append(f"{e}=%s" if alt.startswith("=") else f"{e} LIKE %s")
            args.append(alt[1:] if alt.startswith("=") else f"%{alt}%")
    return ("(" + " OR ".join(parts) + ")", args) if parts else ("1", [])


def _filter(c: dict, v) -> tuple[str | None, list]:
    e = c["expr"]
    if c["kind"] == "enum":                       # ticked values, '-' = empty
        vals = [x for x in v if x != "-"]
        parts = ([f"{e} IN ({', '.join(['%s'] * len(vals))})"] if vals else []) + \
                ([f"{e} IS NULL OR {e}=''"] if "-" in v else [])
        return "(" + " OR ".join(parts) + ")", vals
    if c["kind"] == "num":
        if v == "-":
            return f"{e} IS NULL", []
        m = NUM_RE.fullmatch(v.strip())
        return (f"{e}{m.group(1) or '='}%s", [m.group(2)]) if m else (None, [])
    return _text_filter(e, v)


def roi_terms(v: str) -> list[str]:
    return [t.strip() for t in v.split(",") if t.strip()]


def _digits(v: str) -> str:
    return re.sub(r"\D", "", v)[:8]


def build_where(view: dict, q: str, filters: dict, special: dict) -> tuple[str, list, list[str]]:
    """WHERE over the view: every word of q in some text column, each column filter, every ROI name, the
    study date range."""
    clauses, args, bad = [], [], []
    texts = [c["expr"] for c in view["columns"] if c["kind"] == "text"]
    for word in q.split():
        clauses.append("(" + " OR ".join(f"{e} LIKE %s" for e in texts) + ")")
        args += [f"%{word}%"] * len(texts)
    by_key = {c["key"]: c for c in view["columns"]}
    for k, v in filters.items():
        sql, a = _filter(by_key[k], v)
        if sql is None:
            bad.append(by_key[k]["label"])
            continue
        clauses.append(sql)
        args += a
    exact = special.get("roi_exact") == "1"
    for name in roi_terms(special.get("roi", "")):
        pat = f'%"{like_escape(name)}"%' if exact else f"%{like_escape(name)}%"
        clauses.append(view["roi_sql"])
        args += [pat] * view["roi_args"]
    if view["date"]:
        lo, hi = _digits(special.get("date_from", "")), _digits(special.get("date_to", ""))
        if lo:
            clauses.append(f"{view['date']} >= %s")
            args.append(lo)
        if hi:                                    # 2021 or 202106 means up to the end of that year / month
            clauses.append(f"LEFT({view['date']}, {len(hi)}) <= %s")
            args.append(hi)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args, bad


def linked_rois(v) -> list[str]:
    """The ROI lists of every RTSTRUCT pointing at an image (GROUP_CONCAT of JSON lists), merged."""
    if not v:
        return []
    try:
        lists = json.loads(f"[{v}]")
    except ValueError:
        return [str(v)]
    out = []
    for got in lists:
        for name in (got if isinstance(got, list) else [got]):
            if str(name) not in out:
                out.append(str(name))
    return out


def _cell(c: dict, r: dict) -> dict:
    v = r[c["key"]]
    if v is None or v == "":
        return {"text": None}
    if c["key"] in ("rois", "linked_rois"):
        names = roi_list(v) if c["key"] == "rois" else linked_rois(v)
        text = ", ".join(names)
        return {"text": text if len(text) <= 120 else text[:120] + " …", "title": f"{len(names)}개: {text}"}
    text = c["labels"].get(str(v), str(v))
    out = {"text": text}
    if c["key"] == "dataset":
        out["href"] = url(f"/datasets/{r['_dataset_uuid']}")
    elif c["key"] in ("series_uid", "series_desc") and r.get("_series_uid"):
        out["href"] = url(f"/series/{r['_series_uid']}")
    elif c["key"] == "ref_series":
        out["href"] = url(f"/series/{v}")
    elif c["key"] == "patient_id":
        out["href"] = url("/search?q=") + quote(str(v))
    return out


def _csv_value(c: dict, v):
    if c["key"] == "rois":
        return ", ".join(roi_list(v))
    if c["key"] == "linked_rois":
        return ", ".join(linked_rois(v))
    return None if v is None else c["labels"].get(str(v), v)


def register_browse_views(app, page, db_conn, to_csv) -> None:
    cache, counts = {}, {}

    def cached(key, fn):
        hit = cache.get(key)
        if not hit or time.monotonic() - hit[0] > VALUES_TTL:
            hit = cache[key] = (time.monotonic(), fn())
        return hit[1]

    def values(conn, c) -> list:
        if c["labels"] and not c["values_sql"]:
            return list(c["labels"])
        return cached(c["values_sql"], lambda: sorted(str(r["v"]) for r in dbmod.query(conn, c["values_sql"])
                                                      if r["v"] not in (None, "")))

    def suggestions(conn) -> dict:
        def rois():
            n = collections.Counter()
            for r in dbmod.query(conn, "SELECT roi_names FROM series WHERE roi_names IS NOT NULL"):
                n.update(set(roi_list(r["roi_names"])))
            return [name for name, _ in n.most_common(ROI_SUGGEST)]
        return {"dataset": cached("datasets", lambda: [r["name"] for r in dbmod.query(
                    conn, "SELECT name FROM datasets ORDER BY name")]),
                "roi": cached("rois", rois)}

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
        filters = {}
        for c in view["columns"]:
            if c["kind"] == "enum":
                got = [v for v in qp.getlist(f"f_{c['key']}") if v.strip()]
                if got:
                    filters[c["key"]] = got
            elif c["kind"] != "linked" and qp.get(f"f_{c['key']}", "").strip():
                filters[c["key"]] = qp[f"f_{c['key']}"].strip()
        special = {k: qp[f"f_{k}"].strip() for k in SPECIAL if qp.get(f"f_{k}", "").strip()}
        sortable = [c["key"] for c in view["columns"] if c["kind"] != "linked"]
        sort = qp.get("sort") if qp.get("sort") in sortable else "dataset"
        return vname, view, shown, filters, special, qp.get("q", "").strip(), sort, qp.get("dir") == "desc"

    def run(conn, view, shown, filters, special, q, sort, desc, limit, offset):
        cols = [c for c in view["columns"] if c["key"] in shown]
        where, args, bad = build_where(view, q, filters, special)
        order = {c["key"]: c["expr"] for c in view["columns"]}[sort]
        sel = ", ".join(f"{c['expr']} AS `{c['key']}`" for c in cols)
        dbmod.query(conn, "SET SESSION group_concat_max_len = 1048576")
        rows = dbmod.query(conn, f"SELECT {sel}, {view['extra']} FROM {view['from']}{where}"
                                 f" ORDER BY {order} {'DESC' if desc else 'ASC'}, {view['tiebreak']}"
                                 f" LIMIT {int(limit)} OFFSET {int(offset)}", args)
        return cols, rows, where, args, bad

    def state_of(vname, view, shown, filters, special, q) -> list:
        default = [c["key"] for c in view["columns"] if c["show"]]
        out = [("view", vname), *([("c", k) for k in shown] if shown != default else [])]
        for k, v in filters.items():
            out += [(f"f_{k}", x) for x in v] if isinstance(v, list) else [(f"f_{k}", v)]
        return out + [(f"f_{k}", v) for k, v in special.items()] + ([("q", q)] if q else [])

    def chips(view, state, filters, special, q) -> list[dict]:
        """One chip per condition, each with the link that drops it."""
        labels = {c["key"]: c for c in view["columns"]}
        out = []

        def without(*pairs):
            return url("/browse?") + urlencode([kv for kv in state if kv not in pairs])
        if q:
            out.append({"label": "검색", "value": q, "drop": without(("q", q))})
        for k, v in filters.items():
            c = labels[k]
            for x in (v if isinstance(v, list) else [v]):
                out.append({"label": c["label"], "value": "(없음)" if x == "-" else c["labels"].get(x, x),
                            "drop": without((f"f_{k}", x))})
        if special.get("roi"):
            out.append({"label": "ROI" + (" (정확히)" if special.get("roi_exact") == "1" else ""),
                        "value": special["roi"],
                        "drop": without(("f_roi", special["roi"]), ("f_roi_exact", special.get("roi_exact")))})
        for k, lab in (("date_from", "Study 날짜 ≥"), ("date_to", "Study 날짜 ≤")):
            if special.get(k) and view["date"]:
                out.append({"label": lab, "value": special[k], "drop": without((f"f_{k}", special[k]))})
        return out

    @app.get("/browse", response_class=HTMLResponse)
    def browse(request: Request):
        vname, view, shown, filters, special, q, sort, desc = parse(request)
        try:
            n = int(request.query_params.get("n", 100))
            p = max(1, int(request.query_params.get("p", 1)))
        except ValueError:
            n, p = 100, 1
        n = n if n in PAGE_SIZES else 100
        conn, err = db_conn()
        ctx = {"views": VIEWS, "vname": vname, "view": view, "shown": shown, "filters": filters,
               "special": special, "q": q, "sort": sort, "desc": desc, "n": n, "p": p, "page_sizes": PAGE_SIZES}
        if conn is None:
            return page(request, "browse.html", db_error=err, cols=[], cells=[], total=0, pages=1, bad=[],
                        choices={}, suggest={}, chips=[], elapsed=0, link=lambda **kw: "", csv_link="",
                        more_open=False, **ctx)
        t0 = time.monotonic()
        try:
            cols, rows, where, args, bad = run(conn, view, shown, filters, special, q, sort, desc, n, (p - 1) * n)
            total = count(conn, view, where, args)
            opts = {c["key"]: values(conn, c) for c in view["columns"] if c["kind"] == "enum"}
            suggest = suggestions(conn)
        finally:
            conn.close()
        state = state_of(vname, view, shown, filters, special, q)

        def link(**kw):
            kw = {"sort": sort, "dir": "desc" if desc else "asc", "n": n, **kw}
            return url("/browse?") + urlencode(state + list(kw.items()))
        more_open = any(not c["main"] for c in view["columns"] if c["key"] in filters) or \
            bool(special.get("date_from") or special.get("date_to"))
        cells = [[_cell(c, r) for c in cols] for r in rows]
        return page(request, "browse.html", db_error=None, cols=cols, cells=cells, total=total,
                    pages=max(1, -(-total // n)), bad=bad, choices=opts, suggest=suggest,
                    chips=chips(view, state, filters, special, q), elapsed=time.monotonic() - t0, link=link,
                    more_open=more_open,
                    csv_link=url("/browse.csv?") + urlencode(state + [("sort", sort), ("dir", "desc" if desc else "asc")]),
                    **ctx)

    @app.get("/browse.csv")
    def browse_csv(request: Request):
        vname, view, shown, filters, special, q, sort, desc = parse(request)
        conn, err = db_conn()
        if conn is None:
            raise HTTPException(503, err)
        try:
            cols, rows, *_ = run(conn, view, shown, filters, special, q, sort, desc, CSV_LIMIT, 0)
        finally:
            conn.close()
        return to_csv([[c["label"] for c in cols]] + [[_csv_value(c, r[c["key"]]) for c in cols] for r in rows],
                      f"management-db-{vname}.csv")
