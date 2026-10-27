"""Freeze a validated plan into run-scoped artifacts with hashes (the bytes later sent and registered)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pydicom

from .inventory import read_dicom_header
from .legacy import n2d
from .plan import validate_plan
from .runs import Run, RunError
from .util import copy_hashed, now_iso, safe_name, sha256_file, sha256_json


def prepare(run: Run) -> dict:
    with run.lock():
        meta = run.meta
        if meta["status"] != "PLANNED":
            raise RunError(f"prepare는 PLANNED 상태에서만 가능합니다 (현재 {meta['status']})")
        inv, plan = run.load("inventory.json"), run.load("plan.json")
        if sha256_json(plan) != meta["plan_sha256"]:
            raise RunError("plan.json이 검증 이후 수정되었습니다 — `dataops validate` 로 다시 검증하세요")
        plan, errors = validate_plan(plan, inv)
        if errors:
            run.store_plan(plan, errors, "prepare refused: plan errors")
            raise RunError("plan 오류로 준비할 수 없습니다:\n- " + "\n- ".join(errors))
        root = Path(inv["input_root"])
        art_root = run.path("artifacts")
        if art_root.exists():           # leftovers of an interrupted prepare; nothing was registered from them
            shutil.rmtree(art_root)
        instances, series_out = [], []
        for idx, s in enumerate(plan["series"], start=1):
            if s.get("excluded"):
                continue
            sdir = art_root / f"S{idx:04d}_{safe_name(s['series_uid'][-24:])}"
            if s["kind"] == "dicom":
                rows, report = _freeze_dicom(root, s, sdir, run.dir), None
            else:
                rows, report = _convert_nifti(root, s, sdir, run.dir)
            instances += rows
            series_out.append({"series_key": s["series_key"], "series_uid": s["series_uid"],
                               "dir": sdir.relative_to(run.dir).as_posix(), "instances": len(rows),
                               "conversion_report": report})
        body = {"run_id": run.id, "run_uuid": meta["run_uuid"], "plan_sha256": sha256_json(plan),
                "plan_revision": meta["revision"], "instances": instances}
        manifest = {"schema_version": 1, "created_at": now_iso(), "manifest_sha256": sha256_json(body),
                    "series": series_out, **body}
        run.save("manifest.json", manifest)
        run.set_status("PREPARED", f"{len(instances)} instances frozen", manifest_sha256=manifest["manifest_sha256"],
                       plan_sha256=body["plan_sha256"])
        return manifest


def _instance_row(path: Path, run_dir: Path, sha: str, source: dict) -> dict:
    ds = pydicom.dcmread(path, stop_before_pixels=True)
    return {"sop_uid": str(ds.SOPInstanceUID), "sop_class_uid": str(ds.SOPClassUID),
            "series_uid": str(ds.SeriesInstanceUID), "study_uid": str(ds.StudyInstanceUID),
            "instance_number": int(ds.InstanceNumber) if ds.get("InstanceNumber") not in (None, "") else None,
            "transfer_syntax": str(ds.file_meta.get("TransferSyntaxUID", "")),
            "path": path.relative_to(run_dir).as_posix(), "sha256": sha, "size": path.stat().st_size,
            "source": source}


def _freeze_dicom(root: Path, s: dict, sdir: Path, run_dir: Path) -> list[dict]:
    rows = []
    for rel in s["sources"]:
        src = root / rel
        src_sha = sha256_file(src)
        ds = read_dicom_header(src)
        if ds is None or str(ds.get("SeriesInstanceUID", "")) != s["series_uid"]:
            raise RunError(f"입력 파일이 계획 이후 바뀌었습니다: {rel}")
        dst = sdir / f"{safe_name(str(ds.SOPInstanceUID), 64)}.dcm"
        sha = copy_hashed(src, dst)
        if sha != src_sha:
            raise RunError(f"복사 검증 실패 (hash 불일치): {rel}")
        rows.append(_instance_row_raw(dst, run_dir, sha, ds, {"kind": "dicom", "rel": rel, "sha256": src_sha}))
    return rows


def _instance_row_raw(path: Path, run_dir: Path, sha: str, ds, source: dict) -> dict:
    ts = ""
    if getattr(ds, "file_meta", None) is not None:
        ts = str(ds.file_meta.get("TransferSyntaxUID", ""))
    num = ds.get("InstanceNumber")
    return {"sop_uid": str(ds.SOPInstanceUID), "sop_class_uid": str(ds.SOPClassUID),
            "series_uid": str(ds.SeriesInstanceUID), "study_uid": str(ds.get("StudyInstanceUID", "")),
            "instance_number": int(num) if num not in (None, "") else None, "transfer_syntax": ts,
            "path": path.relative_to(run_dir).as_posix(), "sha256": sha, "size": path.stat().st_size,
            "source": source}


def _convert_nifti(root: Path, s: dict, sdir: Path, run_dir: Path) -> list[dict]:
    conv, rel = s["conversion"], s["sources"][0]
    src = root / rel
    if sha256_file(src) != conv["source_sha256"]:
        raise RunError(f"NIfTI 입력이 계획 이후 바뀌었습니다: {rel}")
    item, vol = n2d.classify(src, root, conv["layout"])
    if item.kind != "image" or vol is None:
        raise RunError(f"NIfTI 재분류 결과가 영상이 아닙니다: {rel} ({item.reason})")
    ids = n2d.SeriesIds(patient_id=s["patient_id"], patient_name=conv["patient_name"], study_uid=s["study_uid"],
                        study_description=conv["study_description"], series_uid=s["series_uid"],
                        series_number=int(s["series_number"]), series_description=s["description"][:64],
                        frame_of_reference_uid=conv["frame_of_reference_uid"])
    report = n2d.write_series(vol, ids, s["modality"], rel, sdir, s["modality_basis"], deterministic=True)
    files = report.pop("files")
    rows = [_instance_row(f, run_dir, sha256_file(f), {"kind": "nifti", "rel": rel, "sha256": conv["source_sha256"],
                                                        "slice_index": i})
            for i, f in enumerate(files)]
    return rows, report


def verify_artifacts(run: Run, manifest: dict | None = None) -> list[str]:
    """Re-hash every frozen artifact; returns problems (empty = intact)."""
    manifest = manifest or run.load("manifest.json")
    if manifest is None:
        return ["manifest 없음 (prepare 전)"]
    body = {k: manifest[k] for k in ("run_id", "run_uuid", "plan_sha256", "plan_revision", "instances")}
    problems = []
    if sha256_json(body) != manifest["manifest_sha256"]:
        problems.append("manifest 내용이 manifest_sha256과 다릅니다")
    for inst in manifest["instances"]:
        p = (run.dir / inst["path"]).resolve()
        if not p.is_relative_to(run.dir.resolve()) or not p.is_file():
            problems.append(f"artifact 없음: {inst['path']}")
        elif sha256_file(p) != inst["sha256"]:
            problems.append(f"artifact 변경됨: {inst['path']}")
    return problems
