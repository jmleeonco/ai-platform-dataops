"""Freeze a validated plan into run-scoped artifacts with hashes (the bytes later sent and registered).

scope "pilot": only the plan's pilot subjects -> manifest-pilot.json (status PILOT_READY), for the trial
               send to the local SCP before approval.
scope "full":  every active series -> manifest.json (status PREPARED); after approval in the browser flow,
               or directly from PLANNED in the CLI (local-only) flow.
Source files are hashed when they are frozen and pinned in sources.json: a source that changes between
the pilot and the full freeze (or between two freezes) is refused. Conversions are deterministic, so a
series already frozen for the pilot is reused byte for byte.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pydicom

from . import deid
from .inventory import read_dicom_header
from .legacy import n2d
from .plan import validate_plan
from .rtstruct import RtstructError, build_rtstruct, rtstruct_ids
from .runs import Run, RunError
from .util import copy_hashed, now_iso, safe_name, sha256_file, sha256_json

MANIFESTS = {"pilot": "manifest-pilot.json", "full": "manifest.json"}
BODY_KEYS = ("run_id", "run_uuid", "plan_sha256", "plan_revision", "scope", "instances")


def manifest_name(scope: str) -> str:
    if scope not in MANIFESTS:
        raise RunError(f"알 수 없는 범위 {scope!r}")
    return MANIFESTS[scope]


def pilot_series(plan: dict) -> list[dict]:
    subjects = set((plan.get("pilot") or {}).get("subjects") or [])
    return [s for s in plan["series"] if not s.get("excluded") and s["subject_key"] in subjects]


def prepare(run: Run, scope: str = "full", log=lambda msg: None) -> dict:
    name = manifest_name(scope)
    with run.lock():
        meta = run.meta
        allowed = {"pilot": {"PLANNED", "PILOT_READY", "PILOT_SENT", "PILOT_PARTIAL", "PILOT_FAILED"},
                   "full": {"PLANNED", "APPROVED"}}[scope]
        if meta["status"] not in allowed:
            raise RunError(f"{scope} 준비는 {sorted(allowed)} 상태에서만 가능합니다 (현재 {meta['status']})")
        inv, plan = run.load("inventory.json"), run.load("plan.json")
        if sha256_json(plan) != meta["plan_sha256"]:
            raise RunError("plan.json이 검증 이후 수정되었습니다 — `dataops validate` 로 다시 검증하세요")
        if meta["status"] == "APPROVED" and (meta.get("approval") or {}).get("plan_sha256") != meta["plan_sha256"]:
            raise RunError("승인된 plan과 현재 plan이 다릅니다")
        plan, errors = validate_plan(plan, inv)
        if errors:
            run.store_plan(plan, errors, f"{scope} prepare refused: plan errors")
            raise RunError("plan 오류로 준비할 수 없습니다:\n- " + "\n- ".join(errors))
        if sha256_json(plan) != meta["plan_sha256"]:
            raise RunError("plan 재검증 결과가 저장된 plan과 다릅니다 — `dataops validate` 로 다시 검증하세요")
        chosen = pilot_series(plan) if scope == "pilot" else [s for s in plan["series"] if not s.get("excluded")]
        if not chosen:
            raise RunError("파일럿 대상자의 시리즈가 없습니다 (plan.pilot.subjects 확인)" if scope == "pilot"
                           else "준비할 시리즈가 없습니다")
        root = Path(inv["input_root"])
        art_root = run.path("artifacts")
        pins = run.load("sources.json", {}) or {}
        reusable = _reusable(run, plan)
        by_key = {s["series_key"]: s for s in plan["series"]}
        instances, series_out, images = [], [], {}
        chosen = sorted(chosen, key=lambda s: s["kind"] == "rtstruct")
        for n, s in enumerate(chosen, start=1):
            log(f"[{n}/{len(chosen)}] {s['series_key']}")
            sdir = art_root / safe_name(s["series_uid"][-40:], 48)
            reused = reusable.get(s["series_uid"])
            # sources are re-hashed even when the artifacts are reused: a changed input is refused
            if reused and all(_pin(pins, root, src) == sha for src, sha in reused["sources"].items()):
                rows, report = reused["instances"], reused["report"]
            else:
                if sdir.exists():
                    shutil.rmtree(sdir)
                if s["kind"] == "dicom":
                    rows, report = _freeze_dicom(root, s, sdir, run.dir, plan), None
                elif s["kind"] == "nifti":
                    rows, report = _convert_nifti(root, s, sdir, run.dir, plan, pins)
                else:
                    rows, report = _convert_rtstruct(root, s, sdir, run.dir, by_key[s["reference"]], pins)
            instances += rows
            series_out.append({"series_key": s["series_key"], "series_uid": s["series_uid"], "kind": s["kind"],
                               "subject_key": s["subject_key"], "dir": sdir.relative_to(run.dir).as_posix(),
                               "instances": len(rows), "conversion_report": report,
                               "sources": {r["source"]["rel"]: r["source"]["sha256"] for r in rows[:1]}})
            run.save("sources.json", pins)
        body = {"run_id": run.id, "run_uuid": meta["run_uuid"], "plan_sha256": sha256_json(plan),
                "plan_revision": meta["revision"], "scope": scope, "instances": instances}
        manifest = {"schema_version": 1, "created_at": now_iso(), "manifest_sha256": sha256_json(body),
                    "series": series_out, **body}
        run.save(name, manifest)
        if scope == "pilot":
            run.set_status("PILOT_READY", f"pilot: {len(instances)} instances frozen "
                                          f"({', '.join(sorted({deid.subject(plan, s['subject_key']) for s in chosen}))})",
                           pilot_manifest_sha256=manifest["manifest_sha256"])
        else:
            run.set_status("PREPARED", f"{len(instances)} instances frozen",
                           manifest_sha256=manifest["manifest_sha256"], plan_sha256=body["plan_sha256"])
        return manifest


def _reusable(run: Run, plan: dict) -> dict:
    """Series frozen by an earlier manifest of the same plan whose files are still intact."""
    out = {}
    for name in MANIFESTS.values():
        m = run.load(name)
        if not m or m.get("plan_sha256") != sha256_json(plan) or verify_artifacts(run, m):
            continue
        for s in m["series"]:
            rows = [i for i in m["instances"] if i["series_uid"] == s["series_uid"]]
            out[s["series_uid"]] = {"instances": rows, "report": s.get("conversion_report"),
                                    "sources": {r["source"]["rel"]: r["source"]["sha256"] for r in rows}}
    return out


def _pin(pins: dict, root: Path, rel: str) -> str:
    """Hash a source file and pin its sha256; a value different from an earlier freeze is refused."""
    sha = sha256_file(root / rel)
    if pins.setdefault(rel, sha) != sha:
        raise RunError(f"입력 파일이 이전 고정(파일럿) 이후 바뀌었습니다: {rel}")
    return sha


def _instance_row(path: Path, run_dir: Path, sha: str, source: dict) -> dict:
    ds = pydicom.dcmread(path, stop_before_pixels=True)
    return _instance_row_raw(path, run_dir, sha, ds, source)


def _freeze_dicom(root: Path, s: dict, sdir: Path, run_dir: Path, plan: dict) -> list[dict]:
    """Copy byte for byte, or with pseudonymisation on, write a pseudonymised copy (deid.dicom)."""
    rows = []
    src_uid = (s.get("source_ids") or {}).get("series_uid", s["series_uid"])
    for rel in s["sources"]:
        src = root / rel
        src_sha = sha256_file(src)
        ds = read_dicom_header(src)
        if ds is None or str(ds.get("SeriesInstanceUID", "")) != src_uid:
            raise RunError(f"입력 파일이 계획 이후 바뀌었습니다: {rel}")
        if deid.enabled(plan):
            ds = deid.dicom(plan, pydicom.dcmread(src, force=True), s["patient_id"])
            if str(ds.SeriesInstanceUID) != s["series_uid"] or str(ds.StudyInstanceUID) != s["study_uid"]:
                raise RunError(f"비식별 UID가 계획과 다릅니다: {rel}")
            dst = sdir / f"{safe_name(str(ds.SOPInstanceUID), 64)}.dcm"
            dst.parent.mkdir(parents=True, exist_ok=True)
            ds.save_as(dst, enforce_file_format=True)
            sha = sha256_file(dst)
            ds = pydicom.dcmread(dst, stop_before_pixels=True)
        else:
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


def image_comments(plan: dict, s: dict) -> str:
    """ImageComments of a series converted from NIfTI (agent-spec plans); the source path is left out when
    pseudonymisation is on."""
    source = "(source path withheld: pseudonymised)" if deid.enabled(plan) else f"'{s['sources'][0]}'"
    return (f"Converted without resampling from NIfTI {source} of the public dataset '{plan['dataset']['name']}'. "
            f"PatientID and UIDs are derived identifiers, not source patient metadata. "
            f"Modality {s['modality']}: {s['modality_basis']}.")


def _convert_nifti(root: Path, s: dict, sdir: Path, run_dir: Path, plan: dict, pins: dict):
    conv, rel = s["conversion"], s["sources"][0]
    src = root / rel
    if conv.get("source_sha256"):
        src_sha = sha256_file(src)
        if src_sha != conv["source_sha256"]:
            raise RunError(f"NIfTI 입력이 계획 이후 바뀌었습니다: {rel}")
        _pin(pins, root, rel)
    else:
        src_sha = _pin(pins, root, rel)
    if plan.get("adapter") == "agent-spec":
        vol, reason = n2d.load_volume(src, conv["layout"])
        if vol is None:
            raise RunError(f"NIfTI를 읽을 수 없습니다: {rel} ({reason})")
        comments = image_comments(plan, s)
    else:
        item, vol = n2d.classify(src, root, conv["layout"])
        if item.kind != "image" or vol is None:
            raise RunError(f"NIfTI 재분류 결과가 영상이 아닙니다: {rel} ({item.reason})")
        comments = None
    ids = n2d.SeriesIds(patient_id=s["patient_id"], patient_name=conv["patient_name"], study_uid=s["study_uid"],
                        study_description=conv["study_description"], series_uid=s["series_uid"],
                        series_number=int(s["series_number"]), series_description=s["description"][:64],
                        frame_of_reference_uid=conv["frame_of_reference_uid"])
    hu = n2d._hu_like(vol)
    report = n2d.write_series(vol, ids, s["modality"], rel, sdir, s["modality_basis"], deterministic=True,
                              image_comments=comments)
    report["modality_check"] = ("ok" if hu == (s["modality"] == "CT") else
                                f"주의: {s['modality']} 로 지정했지만 값 분포는 "
                                f"{'CT(HU, 공기 ≤ -900)' if hu else 'CT(HU)가 아님'}")
    files = report.pop("files")
    rows = [_instance_row(f, run_dir, sha256_file(f), {"kind": "nifti", "rel": rel, "sha256": src_sha,
                                                        "slice_index": i})
            for i, f in enumerate(files)]
    return rows, report


def _convert_rtstruct(root: Path, s: dict, sdir: Path, run_dir: Path, image: dict, pins: dict):
    from .spec import header_geometry               # local import: spec imports plan/agent
    conv, rel = s["conversion"], s["sources"][0]
    src_sha = _pin(pins, root, rel)
    img_conv = image["conversion"]
    geo = header_geometry(root / image["sources"][0], img_conv["layout"])
    sop_class = str(n2d.SOP_CLASS_BY_MODALITY[image["modality"]])
    ctx = {"patient_id": image["patient_id"], "patient_name": img_conv["patient_name"],
           "study_uid": image["study_uid"], "study_description": img_conv["study_description"],
           "series_uid": image["series_uid"], "frame_of_reference_uid": img_conv["frame_of_reference_uid"],
           "sop_class_uid": sop_class, "affine": np.asarray(geo["affine"]), "shape": geo["shape"],
           "sop_uids": [n2d._uid("sop", image["series_uid"], k) for k in range(geo["shape"][2])]}
    series_uid, sop_uid = rtstruct_ids(image["series_uid"], conv["identity"])
    if series_uid != s["series_uid"]:
        raise RunError(f"RTSTRUCT UID 불일치: {s['series_key']}")
    out = sdir / "RS0001.dcm"
    try:
        report = build_rtstruct(root / rel, conv["layout"], ctx, {int(k): v for k, v in conv["label_names"].items()},
                                series_uid, sop_uid, int(s["series_number"]), s["description"], out)
    except RtstructError as e:
        raise RunError(f"RTSTRUCT 변환 실패 {rel}: {e}") from None
    return [_instance_row(out, run_dir, sha256_file(out), {"kind": "rtstruct", "rel": rel, "sha256": src_sha})], report


def verify_artifacts(run: Run, manifest: dict | None = None) -> list[str]:
    """Re-hash every frozen artifact; returns problems (empty = intact)."""
    manifest = manifest or run.load("manifest.json")
    if manifest is None:
        return ["manifest 없음 (prepare 전)"]
    body = {k: manifest[k] for k in BODY_KEYS if k in manifest}
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
