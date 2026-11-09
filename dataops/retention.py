"""Images are transient on this host (CONTRACT §78.6, U155, owner 2026-11-09): "the db agent should not store image
permanently. The images should be in central scp. ... It may store one project's image (if necessary) at a time."

A run's pixel files (everything under artifacts/, and any *.dcm / *.nii / *.nii.gz in the run folder) are working
copies: the durable copy is Central for a published run, the source folder for any other (prepare is deterministic
from source + plan.json, and the manifest's sha256 proves a regeneration). So:

  - purge_images() deletes them and keeps the record (run.json, plan, manifests, journals, report, metadata caches),
    after saving the first header of every frozen series (no pixel or contour data) to purged_headers.json so the
    run page keeps showing the real outgoing values;
  - one slot: check_slot() refuses to freeze or send a run while another run still holds pixel files;
  - automatic purge: a run that reached CENTRAL_SENT with every instance accepted and its registration written,
    a run that ended ANALYSIS_FAILED, and an older run of the same input folder once a new one is started.
    Pilot-only runs are never purged automatically otherwise: the owner frees them (web action or `purge`).

Runs between approval and a complete Central send keep their bytes: they are the approved artifacts, and a
full freeze cannot be repeated after approval.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pydicom

from .runs import Run, RunError, list_runs
from .util import now_iso

PIXEL_SUFFIXES = (".dcm", ".nii", ".nii.gz")
ARTIFACT_DIRS = ("artifacts",)
PURGED_HEADERS = "purged_headers.json"
MANIFEST_FILES = ("manifest-pilot.json", "manifest.json")
PILOT_ONLY = {"PLANNED", "NEEDS_INPUT", "PILOT_READY", "PILOT_SENT", "PILOT_PARTIAL", "PILOT_FAILED"}
_DROP = ("PixelData", "FloatPixelData", "DoubleFloatPixelData", "ROIContourSequence")
_ACCEPTED = ("accepted", "warning")


# --------------------------------------------------------------------------- what a run holds
def pixel_files(run_dir: Path):
    """Every pixel-bearing file of a run folder (artifact directories whole, and image files anywhere)."""
    if not run_dir.is_dir():
        return
    for dirpath, _dirs, files in os.walk(run_dir):
        rel = Path(dirpath).relative_to(run_dir).parts
        in_artifacts = bool(rel) and rel[0] in ARTIFACT_DIRS
        for f in files:
            if in_artifacts or f.lower().endswith(PIXEL_SUFFIXES):
                yield Path(dirpath) / f


def holds_images(run_dir: Path) -> bool:
    return next(pixel_files(run_dir), None) is not None


def footprint(run_dir: Path) -> dict:
    n = size = 0
    for p in pixel_files(run_dir):
        try:
            size += p.stat().st_size
            n += 1
        except OSError:
            pass
    return {"files": n, "bytes": size}


def state(run: Run, meta: dict | None = None) -> dict:
    """The run page's retention line: held locally (files, bytes), or purged (when, where the images are)."""
    meta = meta or run.meta
    fp = footprint(run.dir)
    purged = meta.get("images_purged")
    return {**fp, "holds": fp["files"] > 0, "purged": purged,
            "on_central": meta.get("status") == "CENTRAL_SENT"}


def mb(n: int) -> str:
    return f"{n / 1e6:,.0f} MB" if n < 1e9 else f"{n / 1e9:,.1f} GB"


# --------------------------------------------------------------------------- one slot
def slot_holders(runs_dir: Path, except_id: str | None = None) -> list[dict]:
    """Runs (other than except_id) that still hold pixel files, newest first."""
    out = []
    for m in list_runs(runs_dir):
        if m["run_id"] != except_id and holds_images(runs_dir / m["run_id"]):
            out.append({"run_id": m["run_id"], "status": m.get("status"), "dataset_name": m.get("dataset_name")})
    return out


def check_slot(run: Run) -> None:
    """Refuse to freeze or send `run` while another run holds images (one dataset's images at a time)."""
    other = slot_holders(run.dir.parent, except_id=run.id)
    if other:
        o = other[0]
        size = mb(footprint(run.dir.parent / o["run_id"])["bytes"])
        raise RunError(f"다른 run이 영상을 갖고 있습니다: {o['run_id']} ({o.get('dataset_name') or '-'}, "
                       f"{o['status']}, {size}). 이 서버는 한 번에 한 데이터셋의 영상만 둡니다 — 그 run의 영상을 "
                       f"먼저 삭제하세요 (/runs/{o['run_id']} 의 '이 서버의 영상 삭제' 또는 "
                       f"`python -m dataops purge {o['run_id']}`).")


# --------------------------------------------------------------------------- purge
def purge_refusal(meta: dict) -> str | None:
    st = meta.get("status")
    if st == "ANALYZING":
        return "분석 중인 run입니다"
    if st == "APPROVED" or (st in ("PREPARED", "CENTRAL_PARTIAL", "CENTRAL_FAILED") and meta.get("approval")):
        return (f"{st}: 승인된 고정 파일은 Central 전송이 끝날 때까지 지울 수 없습니다 "
                f"(승인 뒤에는 다시 만들 수 없습니다) — 전송을 이어서 끝내세요")
    return None


def _first_instances(manifest: dict) -> list[dict]:
    """The instances the readers open per series: the lowest instance number and the first listed."""
    low, first = {}, {}
    for inst in manifest.get("instances", []):
        u = inst["series_uid"]
        first.setdefault(u, inst)
        if u not in low or (inst.get("instance_number") or 0) < (low[u].get("instance_number") or 0):
            low[u] = inst
    return list({i["path"]: i for i in [*first.values(), *low.values()]}.values())


def _save_headers(run: Run) -> int:
    """Keep the header (no pixel data, no contour data) of the first instance of every frozen series."""
    cache = run.load(PURGED_HEADERS) or {"files": {}}
    for name in MANIFEST_FILES:
        m = run.load(name)
        for inst in _first_instances(m) if m else []:
            p = run.dir / inst["path"]
            if inst["path"] in cache["files"] or not p.is_file():
                continue
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True)
            except Exception:  # noqa: BLE001 - an unreadable artifact has no header to keep
                continue
            for kw in _DROP:
                if kw in ds:
                    delattr(ds, kw)
            cache["files"][inst["path"]] = ds.to_json_dict(bulk_data_threshold=1 << 20,
                                                            bulk_data_element_handler=lambda el: "")
    cache["saved_at"] = now_iso()
    run.save(PURGED_HEADERS, cache)
    return len(cache["files"])


def header(run: Run, rel: str):
    """The DICOM header of a frozen file: from the file while it exists, else from purged_headers.json."""
    p = run.dir / rel
    if p.is_file():
        return pydicom.dcmread(p, stop_before_pixels=True)
    cache = run.path(PURGED_HEADERS)
    try:
        js = _kept_headers(str(cache), cache.stat().st_mtime_ns).get(rel)
    except OSError:
        js = None
    if js is None:
        raise FileNotFoundError(rel)
    return pydicom.Dataset.from_json(js)


@functools.lru_cache(maxsize=4)
def _kept_headers(path: str, mtime_ns: int) -> dict:
    """purged_headers.json parsed once per version (a full run's is tens of MB, read once per series)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("files") or {}


def purge_images(run: Run, reason: str, by: str = "") -> dict:
    """Delete the run's pixel files; keep the record. Idempotent: nothing to delete -> no change, no event."""
    with run.lock():
        meta = run.meta
        why = purge_refusal(meta)
        if why:
            raise RunError(f"영상을 지울 수 없습니다: {why}")
        fp = footprint(run.dir)
        if not fp["files"]:
            return {"run_id": run.id, "files": 0, "bytes": 0, "already": True, "purged": meta.get("images_purged")}
        headers = _save_headers(run)
        for d in ARTIFACT_DIRS:
            shutil.rmtree(run.dir / d, ignore_errors=True)
        for p in list(pixel_files(run.dir)):
            p.unlink(missing_ok=True)
        left = footprint(run.dir)
        if left["files"]:
            raise RunError(f"영상 파일 {left['files']}개를 지우지 못했습니다")
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": fp["files"], "bytes": fp["bytes"], "reason": reason[:200],
               "by": (by or "")[:64], "status": meta.get("status"), "headers_kept": headers}
        meta = run.meta
        meta["images_purged"] = rec
        meta["events"].append({"at": rec["at"], "status": meta.get("status"), "event": "images_purged",
                               "files": rec["files"], "bytes": rec["bytes"], "reason": rec["reason"]})
        run.save("run.json", meta)
        return {"run_id": run.id, **rec}


# --------------------------------------------------------------------------- automatic purge
def central_complete(run: Run) -> bool:
    """CENTRAL_SENT, every instance of the full manifest accepted by Central, and the registration written."""
    meta, manifest = run.meta, run.load("manifest.json")
    if meta.get("status") != "CENTRAL_SENT" or not manifest or not run.load("registration.json"):
        return False
    from .local_send import journals
    for j in journals(run):
        if (j.get("target") or {}).get("kind") == "central" and j.get("manifest_sha256") == manifest["manifest_sha256"]:
            ok = {u for u, v in j["instances"].items() if v.get("status") in _ACCEPTED}
            return all(i["sop_uid"] in ok for i in manifest["instances"])
    return False


def purge_if_final(run: Run, log=lambda msg: None) -> dict | None:
    """After a publish job: purge when Central holds everything and the DB has the run; else keep (and say why)."""
    if not central_complete(run):
        log("영상 유지: Central 전송 또는 DB 등록이 완료되지 않았습니다")
        return None
    r = purge_images(run, "Central 전송·DB 등록 완료 (자동)")
    log(f"이 서버의 영상 삭제: {r['files']} 파일, {mb(r['bytes'])}")
    return r


def purge_failed(run: Run) -> dict | None:
    if run.meta.get("status") == "ANALYSIS_FAILED" and holds_images(run.dir):
        return purge_images(run, "분석 실패 (자동)")
    return None


def purge_superseded(new: Run) -> list[dict]:
    """A new analysis of the same input folder supersedes older pilot-stage runs: their images go."""
    out, duuid = [], new.meta.get("dataset_uuid")
    for m in list_runs(new.dir.parent):
        if m["run_id"] == new.id or m.get("dataset_uuid") != duuid or m["run_id"] > new.id:
            continue
        old = Run(new.dir.parent, m["run_id"])
        if (m.get("status") in PILOT_ONLY | {"ANALYSIS_FAILED"} or m.get("status") is None) and holds_images(old.dir):
            try:
                out.append(purge_images(old, f"새 run {new.id}로 대체됨 (자동)"))
            except RunError:
                pass                       # busy: the slot check names it later
    return out
