"""Register the model training bundles of /mnt/deploy/data in the management DB (owner 2026-11-03).

A bundle is a model version folder (<model>/<version>/ or <model>/) with train_valid_dict.json (the train/valid
lists) and usually a dicom/ folder holding the DICOM it was built from. Each bundle becomes one dataset; its
series are every DICOM series found in its dicom/ folder (one header per file, no pixels; image series and
RTSTRUCT alike, with ROI names and the referenced series), plus the series its json names by UID that the folder
does not hold — those come from the DB when already registered, else from a read-only C-FIND on Central. Patients
come from the DICOM headers (json-only bundles: from the json). Not pseudonymised (owner: "모두 가명화 필요없어").

/mnt/deploy is read only. Each bundle's folder scan is cached under runtime/deploy_scan/ so a re-run reads only new
bundles. Series and studies this system or the existing-DB import registered are never overwritten; they only
gain the bundle link.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pydicom

from .records import _text, head_values, series_columns, subject_uuid
from .util import now_iso, read_json, write_json

ROOT = Path("/mnt/deploy/data")
SOURCE_KIND, FOUND_KIND = "deploy", "central-find"   # series.source_kind: read from a bundle file / found on Central
DICOM_UID = re.compile(r"[0-9]+(\.[0-9]+)+")
SCAN_TAGS = ["SeriesInstanceUID", "Modality"]


def bundles(root: Path | None = None) -> list[Path]:
    """Folders with a train_valid_dict.json: <model>/<version>/ or <model>/."""
    root = root or ROOT
    found = {p.parent for p in root.glob("*/*/train_valid_dict.json")} | {p.parent for p in root.glob("*/train_valid_dict.json")}
    return sorted(found)


def dataset_uuid(rel: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:ai-platform-dataops:deploy:{rel}"))


# ------------------------------------------------------------------------------------------------ the json
def json_entries(bundle: Path) -> list[dict]:
    """[{split, patient_id, patient_name, image_uid, rtss_uids}] from train_valid_dict.json (DICOM-shaped UIDs only)."""
    d = json.loads((bundle / "train_valid_dict.json").read_text(encoding="utf-8"))
    out = []
    for split, items in d.items() if isinstance(d, dict) else []:
        for e in items if isinstance(items, list) else []:
            if not isinstance(e, dict):
                continue
            rt = e.get("rtss_series_instance_uid")
            rt = rt if isinstance(rt, list) else [rt] if rt else []
            img = e.get("dicom_series_instance_uid")
            out.append({"split": split, "patient_id": e.get("patient_id") or e.get("patientId"),
                        "patient_name": e.get("patient_name"),
                        "image_uid": img if isinstance(img, str) and DICOM_UID.fullmatch(img) else None,
                        "rtss_uids": [u for u in rt if isinstance(u, str) and DICOM_UID.fullmatch(u)]})
    return out


def bundle_info(bundle: Path) -> dict:
    info = read_json(bundle / "deploy_info.json") or {}
    deploys = info.get("deploys") or ([info] if info.get("memo") else [])
    last = deploys[-1] if deploys else {}
    return {"memo": last.get("memo"), "deployed_at": last.get("deployed_at")}


# ------------------------------------------------------------------------------------------------ the folder
def _read(path: str):
    try:
        return path, pydicom.dcmread(path, stop_before_pixels=True, specific_tags=SCAN_TAGS)
    except Exception:  # noqa: BLE001 - not DICOM / unreadable: counted, skipped
        return path, None


def scan_folder(bundle: Path, cache_dir: Path, log=print, workers: int = 16) -> dict:
    """{series_uid: {...}} for every DICOM series under bundle/dicom, cached per bundle."""
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(bundle.relative_to(bundle.anchor)))
    cache = cache_dir / f"{key}.json"
    hit = read_json(cache)
    if hit is not None:
        return hit
    folder = bundle / "dicom"
    series: dict[str, dict] = {}
    stats = Counter()
    if folder.is_dir():
        paths = [os.path.join(d, f) for d, _, fs in os.walk(folder) for f in sorted(fs)]
        log(f"  {len(paths):,}개 파일 읽는 중")
        with ThreadPoolExecutor(workers) as pool:
            for i, (path, ds) in enumerate(pool.map(_read, paths), 1):
                if ds is None or "SeriesInstanceUID" not in ds:
                    stats["DICOM 아님/읽기 실패"] += 1
                    continue
                uid = str(ds.SeriesInstanceUID)
                s = series.get(uid)
                if s is None:                     # the first file of a series: its full header
                    full = pydicom.dcmread(path, stop_before_pixels=True)
                    s = series[uid] = {"head": head_values(full), "files": 0,
                                       "dir": os.path.relpath(os.path.dirname(path), bundle),
                                       **{k: _text(full.get(k), 1024) for k in (
                                           "StudyInstanceUID", "PatientID", "PatientName", "Modality",
                                           "SeriesDescription", "StudyDate", "StudyDescription")},
                                       "SeriesNumber": full.get("SeriesNumber")}
                    if s["SeriesNumber"] is not None:
                        try:
                            s["SeriesNumber"] = int(s["SeriesNumber"])
                        except (TypeError, ValueError):
                            s["SeriesNumber"] = None
                s["files"] += 1
                if i % 20000 == 0:
                    log(f"  {i:,}/{len(paths):,}")
    out = {"scanned_at": now_iso(), "folder": str(folder), "series": series, "stats": dict(stats)}
    write_json(cache, out)
    return out


# ------------------------------------------------------------------------------------------------ the rows
def _series_row(uid: str, s: dict, kind: str, bundle_rel: str) -> dict:
    h = s.get("head") or {}
    row = {"series_uid": uid, "study_uid": s["StudyInstanceUID"], "modality": (s.get("Modality") or "OT")[:16],
           "description": s.get("SeriesDescription"), "series_number": s.get("SeriesNumber"),
           "instance_count": s.get("files") or 0, "source_kind": kind,
           "modality_basis": "DICOM Modality (배포 묶음 파일)" if kind == SOURCE_KIND else "Central C-FIND Modality",
           "metadata_json": {"deploy": {"bundle": bundle_rel, "dir": s.get("dir")}} if kind == SOURCE_KIND
           else {"central_find": {"bundle": bundle_rel}},
           **series_columns({}, h)}
    return row


def central_series(m: dict) -> dict:
    """A C-FIND match in the shape of a scanned series."""
    num = m.get("SeriesNumber")
    return {**{k: m.get(k) for k in ("StudyInstanceUID", "PatientID", "PatientName", "Modality", "SeriesDescription",
                                     "StudyDate", "StudyDescription")},
            "SeriesNumber": int(num) if num and str(num).lstrip("-").isdigit() else None,
            "files": int(m.get("NumberOfSeriesRelatedInstances") or 0),
            "head": {k: m[k] for k in ("SeriesDate", "BodyPartExamined", "PatientSex", "PatientAge") if m.get(k)}}


def build_rows(entries_by_bundle: dict, scans: dict, found: dict, existing: dict) -> tuple[dict, dict]:
    """entries_by_bundle {rel: (info, entries)}; scans {rel: scan}; found {uid: central match}; existing
    {"series": set, "studies": set} registered by something else (never overwritten)."""
    rows = {t: [] for t in ("datasets", "subjects", "studies", "series", "dataset_series")}
    report = {}
    series_seen, study_seen = set(existing["series"]), set(existing["studies"])
    subj_rows = {}
    for rel, (info, entries) in entries_by_bundle.items():
        du = dataset_uuid(rel)
        splits = Counter(e["split"] for e in entries)
        desc = " · ".join(filter(None, [info.get("memo"), "train_valid_dict.json: " + ", ".join(
            f"{k} {v}" for k, v in splits.items())]))
        rows["datasets"].append({"dataset_uuid": du, "name": f"deploy/{rel}", "description": desc,
                                 "source_url": str(ROOT / rel), "license": None, "commercial_use": "U"})
        links, stat = set(), Counter()

        def subject(pid, name=None, h=None):
            su = subject_uuid(du, pid)
            if su not in subj_rows:
                subj_rows[su] = {"subject_uuid": su, "dataset_uuid": du, "source_subject_key": pid, "patient_id": pid[:64],
                                 "patient_name": _text(name, 256), "patient_sex": _text((h or {}).get("PatientSex"), 32),
                                 "patient_age": _text((h or {}).get("PatientAge"), 16),
                                 "patient_weight": _text((h or {}).get("PatientWeight"), 16),
                                 "patient_ethnicity": _text((h or {}).get("EthnicGroup"), 64)}
            return su

        def add(uid, s, kind):
            links.add(uid)
            if uid in series_seen:
                stat["이미 DB에 있는 시리즈 (연결만)"] += 1
                return
            if not s.get("StudyInstanceUID") or not s.get("PatientID") or len(uid) > 64 or len(s["StudyInstanceUID"]) > 64:
                stat["Study/환자 정보 없음 또는 UID 64자 초과"] += 1
                links.discard(uid)
                return
            su = subject(s["PatientID"], s.get("PatientName"), s.get("head"))
            if s["StudyInstanceUID"] not in study_seen:
                date = s.get("StudyDate")
                rows["studies"].append({"study_uid": s["StudyInstanceUID"], "subject_uuid": su,
                                        "study_date": date if date and len(date) == 8 and date.isdigit() else None,
                                        "description": _text(s.get("StudyDescription"), 1024)})
                study_seen.add(s["StudyInstanceUID"])
            rows["series"].append(_series_row(uid, s, kind, rel))
            series_seen.add(uid)
            stat["새 시리즈 (파일)" if kind == SOURCE_KIND else "새 시리즈 (Central 조회)"] += 1

        for uid, s in (scans.get(rel) or {}).get("series", {}).items():
            add(uid, s, SOURCE_KIND)
        named = {u for e in entries for u in ([e["image_uid"]] if e["image_uid"] else []) + e["rtss_uids"]}
        for uid in sorted(named - links):
            if uid in series_seen:
                add(uid, {}, SOURCE_KIND)
            elif uid in found:
                add(uid, found[uid], FOUND_KIND)
            else:
                stat["json의 UID가 폴더·DB·Central 어디에도 없음"] += 1
        if not links:                               # a json-only bundle: its patients at least
            for e in entries:
                if e["patient_id"]:
                    subject(str(e["patient_id"]), e.get("patient_name"))
            stat["시리즈 없이 환자만 (json에 UID·폴더 없음)"] = len({e["patient_id"] for e in entries if e["patient_id"]})
        rows["dataset_series"] += [{"dataset_uuid": du, "series_uid": u} for u in sorted(links)]
        report[rel] = {"entries": len(entries), "series": len(links), **stat}
    rows["subjects"] = list(subj_rows.values())
    return rows, report


def run_import(settings, rescan: bool = False, dry_run: bool = False, log=print) -> dict:
    from . import db as dbmod
    from .central_find import SeriesFinder
    from .legacy_import import upsert
    cache_dir = settings.runtime_dir / "deploy_scan"
    if rescan and cache_dir.is_dir():
        for p in cache_dir.glob("*.json"):
            p.unlink()
    entries, scans = {}, {}
    for b in bundles():
        rel = str(b.relative_to(ROOT))
        entries[rel] = (bundle_info(b), json_entries(b))
        log(f"{rel}: json {len(entries[rel][1]):,}항목")
        scans[rel] = scan_folder(b, cache_dir, log)
    with dbmod.connection(settings.db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT series_uid, study_uid FROM series WHERE source_kind NOT IN (%s, %s)",
                        (SOURCE_KIND, FOUND_KIND))
            got = cur.fetchall()
        existing = {"series": {r["series_uid"] for r in got}, "studies": {r["study_uid"] for r in got}}
        in_folders = {u for s in scans.values() for u in s["series"]}
        named = {u for _, es in entries.values() for e in es
                 for u in ([e["image_uid"]] if e["image_uid"] else []) + e["rtss_uids"]}
        ask = sorted(named - in_folders - existing["series"])
        found_file = cache_dir / "central_find.json"
        found = read_json(found_file) or {}
        todo = [u for u in ask if u not in found]
        if todo:
            log(f"Central C-FIND {len(todo):,}개 시리즈")
            with SeriesFinder(settings.central) as f:
                for i, u in enumerate(todo, 1):
                    m = f.find(u)
                    found[u] = m[0] if m else None
                    if i % 500 == 0:
                        log(f"  {i:,}/{len(todo):,}")
                        write_json(found_file, found)
            write_json(found_file, found)
        found = {u: central_series(m) for u, m in found.items() if m}
        rows, report = build_rows(entries, scans, found, existing)
        summary = {t: len(r) for t, r in rows.items()}
        if not dry_run:
            try:
                with conn.cursor() as cur:
                    for t in ("datasets", "subjects", "studies", "series", "dataset_series"):
                        upsert(cur, t, rows[t])
                        log(f"관리 DB {t}: {len(rows[t]):,}행")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return {"rows": summary, "central_find": {"asked": len(ask), "found": sum(1 for u in ask if u in found)},
            "bundles": report}
