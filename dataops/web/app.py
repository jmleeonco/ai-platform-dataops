"""Local HTML app: start an analysis from a folder, read the agent's report, pilot 2 cases to the local SCP,
approve, publish to Central; browse runs, extracted metadata, proposed vs registered DB rows, deliveries,
prepared slices (with RTSTRUCT contours), DB tables and nightly backups. Mutations live in actions.py and
run as worker jobs."""
from __future__ import annotations

import csv
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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from .. import db as dbmod
from ..compare import sample_view
from ..config import Settings
from ..jobs import active_job, job_log, list_jobs
from ..local_send import journals
from ..metadata import build as build_metadata
from ..report import INDEX_COLUMNS, TITLES, dataset_index, db_preview, dicom_columns, source_columns
from ..report import build as build_report
from ..prepare import MANIFESTS
from ..publish import central_ready
from ..records import TABLES
from ..rtstruct import read_overlay
from ..runs import EDITABLE, Run, RunError, list_runs
from ..util import RUN_ID_RE, valid_uid
from .actions import CSRF_COOKIE, SPLIT_NAMES, register_actions
from .dbview import register_db_views

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
    app.add_middleware(GZipMiddleware, minimum_size=50_000)
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
    def run_page(request: Request, run_id: str):
        """Two actions (Local SCP, approval → Central) and three tables: extracted values, DB rows, DICOM values."""
        run = get_run(run_id)
        meta, plan = run.meta, run.load("plan.json", {}) or {}
        pilot = run.load("manifest-pilot.json")
        jobs = list_jobs(settings, run.id, limit=1)
        job = jobs[0] if jobs else None
        active = active_job(settings, run.id)
        editable = (meta["status"] in EDITABLE and meta["status"] not in ("ANALYZING", "ANALYSIS_FAILED")
                    and not active)
        report = build_report(run, settings) if plan.get("series") else None
        secs = {s["no"]: s for s in (report or {}).get("sections", []) if s["no"] != 10}
        return page(request, "run.html", run=run, meta=meta, plan=plan, pilot=pilot,
                    pilot_stale=bool(pilot and pilot.get("plan_sha256") != meta.get("plan_sha256")),
                    journals=journals(run), plan_errors=run.load("plan_errors.json", []),
                    blocking=[r[2] for r in (secs.get(4) or {}).get("rows", []) if r[1] == "오류"],
                    registration=run.load("registration.json"), summary=_summary(plan),
                    subjects=sorted({x["subject_key"] for x in plan.get("series", []) if not x.get("excluded")}),
                    job=job, job_log=job_log(settings, job["id"], 15) if job else [], active=active,
                    editable=editable, central=central_ready(settings.central), split_names=SPLIT_NAMES,
                    db_sec=secs.get(7), sample=sample_view(run) if plan.get("series") else {"ready": False},
                    header_secs=[s for s in (report or {}).get("sections", []) if s["no"] == 10 and s["rows"]])

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    def report_page(request: Request, run_id: str, refresh: bool = False):
        run = get_run(run_id)
        return page(request, "report.html", run=run, report=build_report(run, settings, refresh), titles=TITLES)

    @app.get("/runs/{run_id}/report.json")
    def report_json(run_id: str):
        return build_report(get_run(run_id), settings)

    @app.get("/runs/{run_id}/metadata")
    def metadata_redirect(run_id: str):
        return RedirectResponse(f"/runs/{get_run(run_id).id}/report#s6", status_code=301)

    def series_tables(run: Run, which: str) -> dict:
        md = build_metadata(run)
        if not md["ready"]:
            raise HTTPException(404)
        if which == "dicom":
            return {"columns": dicom_columns(), "rows": md["dicom"]["rows"],
                    "filters": ["split", "kind", "pilot", "origin", "check"],
                    "dataset": (md["plan"].get("dataset") or {}).get("name")}
        return {"columns": source_columns(md), "rows": md["rows"], "filters": ["split", "kind", "included", "pilot"],
                "dataset": (md["plan"].get("dataset") or {}).get("name")}

    @app.get("/runs/{run_id}/dicom.json")
    def dicom_json(run_id: str):
        return series_tables(get_run(run_id), "dicom")

    @app.get("/runs/{run_id}/series.json")
    def series_json(run_id: str):
        return series_tables(get_run(run_id), "series")

    @app.get("/runs/{run_id}/dicom.csv")
    def dicom_csv(run_id: str):
        return table_csv(get_run(run_id), "dicom")

    @app.get("/runs/{run_id}/series.csv")
    def series_csv(run_id: str):
        return table_csv(get_run(run_id), "series")

    def table_csv(run: Run, which: str):
        t = series_tables(run, which)
        return _csv([["데이터셋"] + [c["label"] for c in t["columns"]]] +
                    [[t["dataset"]] + [r.get(c["key"]) for c in t["columns"]] for r in t["rows"]],
                    f"{which}-{run.id}.csv")

    def live_preview(run: Run) -> dict:
        """Table 7 with each row's state in the DB as of now (the report polls this)."""
        conn, err = db_conn()
        try:
            return db_preview(run, conn, err)
        except Exception as e:  # noqa: BLE001 - show without the DB state rather than fail
            return db_preview(run, None, f"DB 비교 실패: {e}")
        finally:
            if conn:
                conn.close()

    @app.get("/runs/{run_id}/records.json")
    def records_json(run_id: str):
        run = get_run(run_id)
        if not (run.load("plan.json") or {}).get("series"):
            raise HTTPException(404)
        return live_preview(run)

    @app.get("/runs/{run_id}/records/{table}.csv")
    def records_csv(run_id: str, table: str):
        if table not in TABLES:
            raise HTTPException(404)
        run = get_run(run_id)
        t = live_preview(run)["tables"][table]
        return _csv([[c["label"] for c in t["columns"]]] + [[r.get(c["key"]) for c in t["columns"]] for r in t["rows"]],
                    f"{table}-{run.id}.csv")

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request):
        return page(request, "reports.html", rows=dataset_index(settings.runs_dir), columns=INDEX_COLUMNS)

    @app.get("/reports.csv")
    def reports_csv():
        return _csv([INDEX_COLUMNS] + dataset_index(settings.runs_dir), "datasets.csv")

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

    # ------------------------------------------------------------------ backups
    @app.get("/backups", response_class=HTMLResponse)
    def backups(request: Request):
        return page(request, "backups.html", backups=_backups(settings),
                    log_tail=_log_tail(settings.backup_root / settings.backup_system / "backup.log"))

    register_actions(app, settings, page, get_run)
    register_db_views(app, settings, page, db_conn, _csv)

    @app.get("/healthz")
    def healthz():
        conn, err = db_conn()
        if conn:
            conn.close()
        return {"ok": True, "db": err or "ok", "runs_dir": str(settings.runs_dir)}

    return app


# ---------------------------------------------------------------------- helpers
def _csv(rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows([[_cell(v) for v in r] for r in rows])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, (list, tuple)):
        return " ".join(_cell(x) for x in v)
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v                      # dataset text must not become a spreadsheet formula
    return str(v)


def _counts(journal: dict) -> dict:
    out = {}
    for rec in journal["instances"].values():
        out[rec["status"]] = out.get(rec["status"], 0) + 1
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
