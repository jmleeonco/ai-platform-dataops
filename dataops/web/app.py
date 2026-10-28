"""Local HTML app: start an analysis from a folder, read the agent's report, pilot 2 cases to the local SCP,
approve, publish to Central; browse runs, extracted metadata, proposed vs registered DB rows, deliveries,
prepared slices (with RTSTRUCT contours), DB tables and nightly backups. Mutations live in actions.py and
run as worker jobs."""
from __future__ import annotations

import functools
import io
import json
import re
import secrets
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pydicom
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from .. import db as dbmod
from ..config import Settings
from ..jobs import active_job, job_log, list_jobs
from ..local_send import journals
from ..prepare import MANIFESTS, pilot_series
from ..publish import central_ready
from ..records import TABLES, build_records, to_db_value
from ..rtstruct import read_overlay
from ..runs import EDITABLE, Run, RunError, list_runs
from ..util import RUN_ID_RE, valid_uid
from .actions import CSRF_COOKIE, SPLIT_NAMES, register_actions

HERE = Path(__file__).resolve().parent
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PAGE = 100
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _short(v, n=14):
    s = "" if v is None else str(v)
    return s if len(s) <= n + 1 else "…" + s[-n:]


def _jsonfmt(v, limit=400):
    if v is None:
        return ""
    if isinstance(v, (dict, list, tuple)):
        s = json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    else:
        s = str(v)
    return s if len(s) <= limit else s[:limit] + " …"


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="AI Platform DataOps", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters.update(short=_short, jsonfmt=_jsonfmt)
    tpl.env.globals.update(version=__version__)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE)
        request.state.csrf = token or secrets.token_urlsafe(24)
        resp = await call_next(request)
        if not token:
            resp.set_cookie(CSRF_COOKIE, request.state.csrf, httponly=True, samesite="strict", path="/")
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def page(request, name, **ctx):
        return tpl.TemplateResponse(request, name, {"settings": settings, "csrf": request.state.csrf,
                                                    "msg": request.query_params.get("msg"),
                                                    "err_msg": request.query_params.get("err"), **ctx})

    def db_conn():
        if not settings.db_configured:
            return None, "DB 설정 없음"
        try:
            return dbmod.connect(settings.db), None
        except dbmod.DbError as e:
            return None, str(e)

    def get_run(run_id: str) -> Run:
        if not RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(404)
        run = Run(settings.runs_dir, run_id)
        if not run.exists:
            raise HTTPException(404)
        return run

    # ------------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        conn, db_error = db_conn()
        db_info = None
        if conn:
            try:
                db_info = {"migrations": dbmod.migration_status(conn), "counts": dbmod.table_counts(conn)}
            finally:
                conn.close()
        runs = list_runs(settings.runs_dir)
        for m in runs:
            run = Run(settings.runs_dir, m["run_id"])
            man = run.load("manifest.json")
            m["instances"] = len(man["instances"]) if man else None
            m["registered"] = (run.load("registration.json") or {}).get("last")
            m["transfers"] = [{"target": j["target_key"], "scope": j.get("scope", "full"), "counts": _counts(j)}
                              for j in journals(run)]
            m["job"] = active_job(settings, m["run_id"])
        return page(request, "index.html", runs=runs, db_info=db_info, db_error=db_error,
                    backups=_backups(settings)[:5], central=central_ready(settings.central))

    # ------------------------------------------------------------------ run
    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str, skipped_page: int = Query(1, ge=1), sp: int = Query(1, ge=1),
                 q: str = ""):
        run = get_run(run_id)
        meta, inv, plan = run.meta, run.load("inventory.json", {}), run.load("plan.json", {}) or {}
        manifest, pilot = run.load("manifest.json"), run.load("manifest-pilot.json")
        jlist = journals(run)
        per_series = _per_series([m for m in (manifest, pilot) if m], jlist)
        records, compare, db_error = None, None, None
        if manifest:
            records = build_records(run)
            conn, db_error = db_conn()
            if conn:
                try:
                    compare = _compare(conn, records)
                except Exception as e:  # noqa: BLE001 - show, don't crash the page
                    db_error = f"DB 비교 실패: {e}"
                finally:
                    conn.close()
        skipped = plan.get("skipped", [])
        series = plan.get("series", [])
        if q:
            series = [x for x in series if q.lower() in (x.get("subject_key") or "").lower()
                      or q.lower() in x["series_key"].lower()]
        jobs = list_jobs(settings, run.id, limit=10)
        job = jobs[0] if jobs else None
        editable = (meta["status"] in EDITABLE and meta["status"] not in ("ANALYZING", "ANALYSIS_FAILED")
                    and not active_job(settings, run.id))
        pilot_stale = bool(pilot and pilot.get("plan_sha256") != meta.get("plan_sha256"))
        return page(request, "run.html", run=run, meta=meta, inv=inv, plan=plan, manifest=manifest, pilot=pilot,
                    pilot_stale=pilot_stale, journals=jlist, per_series=per_series, records=records, compare=compare,
                    db_error=db_error, plan_errors=run.load("plan_errors.json", []),
                    registration=run.load("registration.json"), tables=TABLES,
                    skipped=skipped[(skipped_page - 1) * PAGE: skipped_page * PAGE], skipped_page=skipped_page,
                    skipped_pages=max(1, -(-len(skipped) // PAGE)), series=series[(sp - 1) * PAGE: sp * PAGE],
                    sp=sp, series_pages=max(1, -(-len(series) // PAGE)), q=q, summary=_summary(plan),
                    subjects=sorted({x["subject_key"] for x in plan.get("series", []) if not x.get("excluded")}),
                    job=job, job_log=job_log(settings, job["id"], 15) if job else [], jobs=jobs,
                    active=active_job(settings, run.id), editable=editable, central=central_ready(settings.central),
                    agent=run.load("agent_proposal.json"), split_names=SPLIT_NAMES,
                    pilot_series_rows=_pilot_rows(plan, pilot, per_series), stage=_stage(meta))

    @app.get("/runs/{run_id}/state.json")
    def run_state(run_id: str):
        run = get_run(run_id)
        job = active_job(settings, run.id)
        return {"status": run.meta["status"], "updated_at": run.meta["updated_at"],
                "job": {"id": job["id"], "kind": job["kind"], "status": job["status"],
                        "log": job_log(settings, job["id"], 8)} if job else None}

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return page(request, "jobs.html", jobs=list_jobs(settings, limit=200))

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = next((j for j in list_jobs(settings, limit=10_000) if j["id"] == job_id), None)
        if job is None:
            raise HTTPException(404)
        return page(request, "job.html", job=job, log=job_log(settings, job_id, 2000))

    @app.get("/runs/{run_id}/series/{series_uid}", response_class=HTMLResponse)
    def series_page(request: Request, run_id: str, series_uid: str):
        run = get_run(run_id)
        if not valid_uid(series_uid):
            raise HTTPException(404)
        plan = run.load("plan.json", {})
        s = next((x for x in plan.get("series", []) if x.get("series_uid") == series_uid), None)
        if s is None:
            raise HTTPException(404)
        instances, report, frozen_in = [], None, None
        for name in MANIFESTS.values():
            manifest = run.load(name)
            if manifest and any(i["series_uid"] == series_uid for i in manifest["instances"]):
                instances = [i for i in manifest["instances"] if i["series_uid"] == series_uid]
                report = next((x.get("conversion_report") for x in manifest["series"]
                               if x["series_uid"] == series_uid), None)
                frozen_in = name
                break
        overlays = []
        if s["kind"] == "nifti" and frozen_in:
            m = run.load(frozen_in)
            for x in plan["series"]:
                if x["kind"] == "rtstruct" and x.get("reference") == s["series_key"]:
                    rs = next((i for i in m["instances"] if i["series_uid"] == x["series_uid"]), None)
                    if rs:
                        overlays.append({"sop_uid": rs["sop_uid"], "series_uid": x["series_uid"],
                                         "legend": _overlay(run, rs["path"])["legend"]})
        if s["kind"] != "rtstruct":
            instances = _slice_order(run, instances)
        status = {}
        for j in journals(run):
            for i in instances:
                rec = j["instances"].get(i["sop_uid"])
                if rec:
                    status.setdefault(i["sop_uid"], []).append(f"{j['target_key']}: {rec['status']}")
        geometry = _geometry(run, instances[0]) if instances else None
        return page(request, "series.html", run=run, s=s, instances=[] if s["kind"] == "rtstruct" else instances,
                    rs_instances=instances if s["kind"] == "rtstruct" else [], report=report, delivery=status,
                    frozen_in=frozen_in, overlays=overlays,
                    geometry=geometry, study=next((st for st in plan["studies"] if st["study_uid"] == s["study_uid"]),
                                                  None))

    @app.get("/runs/{run_id}/instances/{sop_uid}.png")
    def slice_png(run_id: str, sop_uid: str, wc: float | None = None, ww: float | None = Query(None, gt=0),
                  rs: str | None = None):
        run = get_run(run_id)
        if not valid_uid(sop_uid) or (rs is not None and not valid_uid(rs)):
            raise HTTPException(404)
        frozen = _frozen(run)
        inst = frozen.get(sop_uid)
        if inst is None:
            raise HTTPException(404)
        path = (run.dir / inst["path"]).resolve()
        if not path.is_relative_to(run.dir.resolve()) or not path.is_file():
            raise HTTPException(404)
        contours = None
        if rs:
            rsi = frozen.get(rs)
            if rsi is None or not rsi["sop_class_uid"].endswith(".481.3"):
                raise HTTPException(404)
            contours = _overlay(run, rsi["path"])["contours"].get(sop_uid, [])
        try:
            png = _render(path, wc, ww, contours)
        except Exception as e:  # noqa: BLE001 - compressed / non-image objects
            return JSONResponse({"detail": f"미리보기 불가: {type(e).__name__}: {e}"}, status_code=422)
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------------------------ DB browsing
    @app.get("/datasets", response_class=HTMLResponse)
    def datasets(request: Request):
        conn, err = db_conn()
        rows = []
        if conn:
            try:
                rows = dbmod.query(conn, """
                    SELECT d.*, (SELECT COUNT(*) FROM subjects s WHERE s.dataset_uuid=d.dataset_uuid) AS n_subjects,
                           (SELECT COUNT(*) FROM dataset_series m WHERE m.dataset_uuid=d.dataset_uuid) AS n_series,
                           (SELECT COALESCE(SUM(se.instance_count),0) FROM dataset_series m JOIN series se
                              ON se.series_uid=m.series_uid WHERE m.dataset_uuid=d.dataset_uuid) AS n_instances
                    FROM datasets d ORDER BY d.created_at DESC""")
            finally:
                conn.close()
        return page(request, "datasets.html", rows=rows, db_error=err)

    @app.get("/datasets/{dataset_uuid}", response_class=HTMLResponse)
    def dataset_page(request: Request, dataset_uuid: str, modality: str | None = None, q: str | None = None,
                     p: int = Query(1, ge=1)):
        if not UUID_RE.fullmatch(dataset_uuid):
            raise HTTPException(404)
        conn, err = db_conn()
        if conn is None:
            return page(request, "datasets.html", rows=[], db_error=err)
        try:
            ds = dbmod.query(conn, "SELECT * FROM datasets WHERE dataset_uuid=%s", (dataset_uuid,))
            if not ds:
                raise HTTPException(404)
            where, args = ["m.dataset_uuid=%s"], [dataset_uuid]
            if modality:
                where.append("se.modality=%s")
                args.append(modality)
            if q:
                where.append("(sj.patient_id LIKE %s OR se.description LIKE %s OR se.series_uid LIKE %s)")
                args += [f"%{q}%"] * 3
            base = (" FROM dataset_series m JOIN series se ON se.series_uid=m.series_uid"
                    " JOIN studies st ON st.study_uid=se.study_uid JOIN subjects sj ON sj.subject_uuid=st.subject_uuid"
                    " WHERE " + " AND ".join(where))
            total = dbmod.query(conn, "SELECT COUNT(*) AS n" + base, args)[0]["n"]
            rows = dbmod.query(conn, "SELECT sj.patient_id, sj.source_subject_key, st.study_uid, st.study_date,"
                                     " se.series_uid, se.modality, se.description, se.instance_count, se.source_kind,"
                                     " se.needs_review, se.modality_basis, se.first_run_uuid, se.metadata_json, "
                                     " (SELECT r.run_label FROM ingest_runs r WHERE r.run_uuid=se.first_run_uuid)"
                                     " AS run_label" + base + " ORDER BY sj.patient_id, st.study_uid, se.series_number"
                                     f" LIMIT {PAGE} OFFSET %s", [*args, (p - 1) * PAGE])
            modalities = dbmod.query(conn, "SELECT se.modality, COUNT(*) AS n FROM dataset_series m JOIN series se"
                                           " ON se.series_uid=m.series_uid WHERE m.dataset_uuid=%s"
                                           " GROUP BY se.modality ORDER BY n DESC", (dataset_uuid,))
            delivery = dbmod.query(conn, "SELECT t.target_key, t.status, COUNT(*) AS n FROM transfer_results t"
                                         " JOIN instances i ON i.sop_uid=t.sop_uid JOIN dataset_series m"
                                         " ON m.series_uid=i.series_uid WHERE m.dataset_uuid=%s"
                                         " GROUP BY t.target_key, t.status", (dataset_uuid,))
        finally:
            conn.close()
        for r in rows:
            r["meta"] = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        return page(request, "dataset.html", ds=ds[0], rows=rows, total=total, p=p, pages=max(1, -(-total // PAGE)),
                    modality=modality or "", q=q or "", modalities=modalities, delivery=delivery)

    @app.get("/db", response_class=HTMLResponse)
    def db_overview(request: Request):
        conn, err = db_conn()
        info = None
        if conn:
            try:
                info = {"migrations": dbmod.migration_status(conn), "counts": dbmod.table_counts(conn),
                        "columns": {t: dbmod.query(conn, "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY "
                                                         "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()"
                                                         " AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (t,))
                                    for t in dbmod.BROWSABLE}}
            finally:
                conn.close()
        return page(request, "db.html", info=info, db_error=err)

    @app.get("/db/{table}", response_class=HTMLResponse)
    def db_table(request: Request, table: str, p: int = Query(1, ge=1)):
        if table not in dbmod.BROWSABLE:
            raise HTTPException(404)
        conn, err = db_conn()
        if conn is None:
            return page(request, "db.html", info=None, db_error=err)
        try:
            total = dbmod.query(conn, f"SELECT COUNT(*) AS n FROM `{table}`")[0]["n"]
            rows = dbmod.query(conn, f"SELECT * FROM `{table}` LIMIT {PAGE} OFFSET %s", ((p - 1) * PAGE,))
        finally:
            conn.close()
        cols = list(rows[0].keys()) if rows else []
        return page(request, "table.html", table=table, rows=rows, cols=cols, total=total, p=p,
                    pages=max(1, -(-total // PAGE)))

    # ------------------------------------------------------------------ backups
    @app.get("/backups", response_class=HTMLResponse)
    def backups(request: Request):
        return page(request, "backups.html", backups=_backups(settings),
                    log_tail=_log_tail(settings.backup_root / settings.backup_system / "backup.log"))

    register_actions(app, settings, page, get_run)

    @app.get("/healthz")
    def healthz():
        conn, err = db_conn()
        if conn:
            conn.close()
        return {"ok": True, "db": err or "ok", "runs_dir": str(settings.runs_dir)}

    return app


# ---------------------------------------------------------------------- helpers
def _counts(journal: dict) -> dict:
    out = {}
    for rec in journal["instances"].values():
        out[rec["status"]] = out.get(rec["status"], 0) + 1
    return out


def _compare(conn, records: dict) -> dict:
    """Per table: how many proposed rows are missing / identical / different in the DB."""
    out = {}
    for table in TABLES:
        rows = records[table]
        keys = dbmod.KEYS[table]
        existing = dbmod._fetch_existing(conn.cursor(), table, rows) if rows else {}
        c = {"missing": 0, "same": 0, "different": 0, "diff_examples": []}
        for row in rows:
            old = existing.get(tuple(row[k] for k in keys))
            if old is None:
                c["missing"] += 1
                continue
            diffs = [col for col, v in row.items() if col not in dbmod.KEEP_FIRST.get(table, set())
                     and dbmod._norm(old.get(col)) != dbmod._norm(to_db_value(col, v))]
            if diffs:
                c["different"] += 1
                if len(c["diff_examples"]) < 5:
                    c["diff_examples"].append({"key": [row[k] for k in keys], "columns": diffs})
            else:
                c["same"] += 1
        out[table] = c
    return out


def _frozen(run: Run) -> dict:
    out = {}
    for name in MANIFESTS.values():
        m = run.load(name)
        for i in (m or {}).get("instances", []):
            out.setdefault(i["sop_uid"], i)
    return out


def _overlay(run: Run, rel: str) -> dict:
    p = (run.dir / rel).resolve()
    if not p.is_relative_to(run.dir.resolve()) or not p.is_file():
        return {"contours": {}, "legend": []}
    return _overlay_cached(str(p), p.stat().st_mtime_ns)


@functools.lru_cache(maxsize=16)
def _overlay_cached(path: str, mtime_ns: int) -> dict:
    return read_overlay(Path(path))


def _per_series(manifests: list[dict], jlist: list[dict]) -> dict:
    """{series_uid: {scope: counts}} over the pilot and full manifests."""
    out = {}
    for m in manifests:
        scope = m.get("scope", "full")
        status_of = defaultdict(list)
        for j in jlist:
            if j.get("scope", "full") == scope and j["manifest_sha256"] == m["manifest_sha256"]:
                for sop, rec in j["instances"].items():
                    status_of[sop].append((j["target"].get("kind", "local"), rec["status"]))
        for inst in m["instances"]:
            c = out.setdefault(inst["series_uid"], {}).setdefault(scope, Counter())
            c["instances"] += 1
            sts = status_of.get(inst["sop_uid"], [])
            if not sts:
                c["unsent"] += 1
            for kind, st in sts:
                c[f"{kind}:{st}"] += 1
    return out


def _summary(plan: dict) -> dict:
    """Counts per split: subjects, CT/MR images, labels; active and left out."""
    rows = defaultdict(lambda: Counter())
    for s in plan.get("series", []):
        split = s.get("split") or (s.get("metadata") or {}).get("split") or "none"
        state = "out" if s.get("excluded") else "in"
        kind = s["modality"] if s["kind"] in ("nifti", "dicom") else "labels"
        rows[split][f"{state}:{kind}"] += 1
        if s["kind"] != "rtstruct":
            rows[split][f"{state}:subjects:{s['subject_key']}"] = 1
    out = {}
    for split, c in rows.items():
        out[split] = {"subjects_in": sum(1 for k in c if k.startswith("in:subjects:")),
                      "subjects_out": sum(1 for k in c if k.startswith("out:subjects:")),
                      **{k: v for k, v in c.items() if ":subjects:" not in k}}
    order = ["train", "validation", "test", "other", "none"]
    return dict(sorted(out.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99))


def _pilot_rows(plan: dict, pilot: dict | None, per_series: dict) -> list[dict]:
    if not plan.get("series"):
        return []
    reports = {x["series_uid"]: x.get("conversion_report") for x in (pilot or {}).get("series", [])}
    rows = []
    for s in pilot_series(plan):
        rows.append({"s": s, "report": reports.get(s["series_uid"]),
                     "counts": per_series.get(s["series_uid"], {}).get("pilot")})
    return rows


def _stage(meta: dict) -> int:
    st = meta.get("status") or ""
    if st in ("ANALYZING", "ANALYSIS_FAILED", None, ""):
        return 0
    if st in ("PLANNED", "NEEDS_INPUT"):
        return 1
    if st.startswith("PILOT_"):
        return 2
    if st == "APPROVED" or (st == "PREPARED" and meta.get("approval")):
        return 3
    if st.startswith("CENTRAL_"):
        return 4
    return 1


def _slice_order(run: Run, instances: list[dict]) -> list[dict]:
    def key(i):
        try:
            ds = pydicom.dcmread(run.dir / i["path"], stop_before_pixels=True,
                                 specific_tags=["ImagePositionPatient", "ImageOrientationPatient", "InstanceNumber"])
            iop = [float(v) for v in ds.ImageOrientationPatient]
            normal = np.cross(iop[:3], iop[3:])
            return (0, float(np.dot(normal, [float(v) for v in ds.ImagePositionPatient])))
        except Exception:  # noqa: BLE001 - no geometry: fall back to instance number
            return (1, i.get("instance_number") or 0)
    if len(instances) > 2000:            # keep the page fast on huge series
        return sorted(instances, key=lambda i: i.get("instance_number") or 0)
    return sorted(instances, key=key)


def _geometry(run: Run, inst: dict) -> dict:
    try:
        ds = pydicom.dcmread(run.dir / inst["path"], stop_before_pixels=True)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for tag in ("Rows", "Columns", "PixelSpacing", "SliceThickness", "ImageOrientationPatient", "PatientPosition",
                "WindowCenter", "WindowWidth", "RescaleSlope", "RescaleIntercept", "PhotometricInterpretation"):
        if tag in ds and ds.get(tag) not in (None, ""):
            v = ds.get(tag)
            out[tag] = [str(x) for x in v] if isinstance(v, pydicom.multival.MultiValue) else str(v)
    iop = ds.get("ImageOrientationPatient")
    if iop is not None and len(iop) == 6:
        normal = np.cross([float(x) for x in iop[:3]], [float(x) for x in iop[3:]])
        axis = int(np.argmax(np.abs(normal)))
        out["plane"] = ["sagittal", "coronal", "axial"][axis]
        row, col = [float(x) for x in iop[:3]], [float(x) for x in iop[3:]]
        out["screen_right"], out["screen_down"] = _lps_dir(row), _lps_dir(col)
        opposite = {"L": "R", "R": "L", "P": "A", "A": "P", "S": "I", "I": "S"}
        right, down = out["screen_right"][:1], out["screen_down"][:1]
        if right and down:
            out["edges"] = {"l": opposite[right], "r": right, "t": opposite[down], "b": down}
    return out


def _lps_dir(v) -> str:
    names = [("L", "R"), ("P", "A"), ("S", "I")]
    return "".join(names[a][0 if v[a] > 0 else 1] for a in sorted(range(3), key=lambda a: -abs(v[a])) if abs(v[a]) > 0.3)


def _render(path: Path, wc, ww, contours=None) -> bytes:
    from PIL import Image, ImageDraw
    ds = pydicom.dcmread(path)
    arr = ds.pixel_array.astype(np.float64)
    if arr.ndim == 3 and ds.get("SamplesPerPixel", 1) == 1:
        arr = arr[arr.shape[0] // 2]
    if arr.ndim != 2:
        raise ValueError(f"2D 단일 채널이 아님 (shape {arr.shape})")
    arr = arr * float(ds.get("RescaleSlope", 1) or 1) + float(ds.get("RescaleIntercept", 0) or 0)
    if wc is None or ww is None:
        lo, hi = np.percentile(arr, [0.5, 99.5])
        wc, ww = (lo + hi) / 2, max(hi - lo, 1.0)
    img = np.clip((arr - (wc - ww / 2)) / ww, 0, 1) * 255
    if ds.get("PhotometricInterpretation") == "MONOCHROME1":
        img = 255 - img
    out = Image.fromarray(img.astype(np.uint8), mode="L")
    if contours:
        out = out.convert("RGB")
        draw = ImageDraw.Draw(out)
        ipp = np.array([float(v) for v in ds.ImagePositionPatient])
        iop = [float(v) for v in ds.ImageOrientationPatient]
        row_dir, col_dir = np.array(iop[:3]), np.array(iop[3:])
        dr, dc = (float(v) for v in ds.PixelSpacing)          # row spacing, column spacing
        for color, pts in contours:
            col = (pts - ipp) @ row_dir / dc
            row = (pts - ipp) @ col_dir / dr
            xy = [(float(c), float(r)) for c, r in zip(col, row)]
            if len(xy) >= 2:
                draw.line(xy + [xy[0]], fill=tuple(color), width=1)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _backups(settings: Settings) -> list[dict]:
    base = settings.backup_root / settings.backup_system
    out = []
    try:
        dirs = sorted((d for d in base.iterdir() if d.is_dir() and DATE_RE.fullmatch(d.name)), reverse=True)
    except OSError:
        return out
    for d in dirs:
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            m = {"ok": False, "error": "manifest.json 없음/손상"}
        out.append({"date": d.name, "ok": m.get("ok"), "finished_at": m.get("finished_at"),
                    "duration_s": m.get("duration_s"), "total_bytes": sum(f.get("size", 0) for f in m.get("files", [])),
                    "files": m.get("files", []), "db_counts": m.get("db_counts"), "commit": m.get("commit"),
                    "error": m.get("error")})
    return out


def _log_tail(path: Path, n: int = 30) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


__all__ = ["create_app", "RunError"]
