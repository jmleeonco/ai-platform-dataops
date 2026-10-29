"""The one projection from a run to DB rows: used both for the HTML preview and for registration."""
from __future__ import annotations

import json
from uuid import UUID, uuid5

from . import deid
from .runs import Run

TABLES = ["datasets", "subjects", "studies", "series", "dataset_series", "ingest_runs", "instances",
          "transfer_results"]


def subject_uuid(dataset_uuid: str, source_subject_key: str) -> str:
    return str(uuid5(UUID(dataset_uuid), source_subject_key))


def _plan_summary(plan: dict) -> dict:
    """ingest_runs.plan_json: the plan without per-file source lists (the full plan stays in the run folder)."""
    return {"schema_version": plan["schema_version"], "adapter": plan["adapter"],
            "adapter_version": plan["adapter_version"], "options": plan["options"],
            "subjects": len(plan["subjects"]), "studies": len(plan["studies"]),
            "series": [{**{k: s.get(k) for k in ("kind", "excluded", "series_uid", "modality", "modality_basis",
                                                  "needs_review", "conversion")},
                        "series_key": deid.path(plan, s["series_key"])} for s in plan["series"]],
            "deid": {k: v for k, v in (plan.get("deid") or {"enabled": False}).items() if k != "key"},
            "skipped": len(plan.get("skipped", [])), "unresolved": plan.get("unresolved", [])}


def build_records(run: Run, preview: bool = False) -> dict:
    """preview=True works before the full freeze: instance counts come from the plan, and the per-file rows
    (instances, transfer_results) stay empty because they only exist once artifacts are frozen and sent."""
    meta, plan, manifest = run.meta, run.load("plan.json"), run.load("manifest.json")
    if plan is None or (manifest is None and not preview):
        raise ValueError("prepare 이후에만 DB 레코드를 만들 수 있습니다")
    if manifest is None:
        manifest = {"manifest_sha256": None, "instances": []}
    ds = plan["dataset"]
    duuid = ds["dataset_uuid"]
    active = [s for s in plan["series"] if not s.get("excluded")]
    counts = {}
    for inst in manifest["instances"]:
        counts[inst["series_uid"]] = counts.get(inst["series_uid"], 0) + 1
    if manifest["manifest_sha256"] is None:
        counts = {s["series_uid"]: s.get("instance_count") or 0 for s in active}
    studies_meta = {st["study_uid"]: st.get("metadata") or {} for st in plan["studies"]}
    subj = {s["subject_key"]: s["patient_id"] for s in active}
    rows = {t: [] for t in TABLES}
    rows["datasets"].append({k: ds.get(k) for k in ("dataset_uuid", "name", "source_url", "license",
                                                    "commercial_use", "description", "patient_id_prefix")})
    for key, pid in sorted(subj.items()):
        out_key = deid.subject(plan, key)
        rows["subjects"].append({"subject_uuid": subject_uuid(duuid, out_key), "dataset_uuid": duuid,
                                 "source_subject_key": out_key, "patient_id": pid})
    seen_studies = set()
    for s in active:
        if s["study_uid"] not in seen_studies:
            seen_studies.add(s["study_uid"])
            m = studies_meta.get(s["study_uid"], {})
            desc = m.get("StudyDescription") or (s["conversion"] or {}).get("study_description")
            rows["studies"].append({"study_uid": s["study_uid"],
                                    "subject_uuid": subject_uuid(duuid, deid.subject(plan, s["subject_key"])),
                                    "study_date": m.get("StudyDate"), "description": desc,
                                    "metadata_json": m or None})
        meta_json = dict(s.get("metadata") or {}) or None
        if meta_json and "label_of" in meta_json:
            meta_json["label_of"] = deid.path(plan, meta_json["label_of"])
        rows["series"].append({"series_uid": s["series_uid"], "study_uid": s["study_uid"], "modality": s["modality"],
                               "description": s.get("description"), "metadata_json": meta_json,
                               "series_number": int(s["series_number"]) if str(s.get("series_number") or "").lstrip(
                                   "-").isdigit() else None,
                               "instance_count": counts.get(s["series_uid"], 0), "source_kind": s["kind"],
                               "modality_basis": s.get("modality_basis"), "needs_review": int(bool(s.get("needs_review"))),
                               "first_run_uuid": meta["run_uuid"]})
        rows["dataset_series"].append({"dataset_uuid": duuid, "series_uid": s["series_uid"]})
    rows["ingest_runs"].append({"run_uuid": meta["run_uuid"], "run_label": run.id, "dataset_uuid": duuid,
                                "revision": meta["revision"], "manifest_sha256": manifest["manifest_sha256"],
                                "status": meta["status"], "plan_json": _plan_summary(plan),
                                "approval_json": meta.get("approval"), "input_root": plan["input_root"]})
    rows["instances"] = instance_rows(run, manifest, plan)
    rows["transfer_results"] = transfer_rows(run, {i["sop_uid"] for i in manifest["instances"]})
    return rows


def instance_rows(run: Run, manifest: dict, plan: dict | None = None) -> list[dict]:
    """instances rows for a frozen manifest (the full one at registration; the pilot one as a preview)."""
    plan = plan if plan is not None else (run.load("plan.json") or {})
    return [{"sop_uid": inst["sop_uid"], "series_uid": inst["series_uid"], "sop_class_uid": inst["sop_class_uid"],
             "artifact_sha256": inst["sha256"], "artifact_relative_path": f"{run.id}/{inst['path']}",
             "prepared_run_uuid": run.meta["run_uuid"], "instance_number": inst["instance_number"],
             "size_bytes": inst["size"], "transfer_syntax_uid": inst["transfer_syntax"] or None,
             "source_relative_path": deid.path(plan, inst["source"]["rel"]), "source_sha256": inst["source"]["sha256"]}
            for inst in manifest["instances"]]


def transfer_rows(run: Run, frozen: set) -> list[dict]:
    """transfer_results rows: every journal entry for a frozen SOP (any target, pilot or full send)."""
    from .local_send import journals  # local import: avoids a cycle at module load
    out = []
    for j in journals(run) if frozen else []:
        for sop, rec in sorted(j["instances"].items()):
            if sop not in frozen:            # e.g. a pilot frozen from an earlier plan revision
                continue
            out.append({"run_uuid": run.meta["run_uuid"], "target_key": j["target_key"], "sop_uid": sop,
                        "target_snapshot": j["target"], "status": rec["status"], "attempts": rec.get("attempts", 0),
                        "response_code": None, "verification_json": None, "error_message": rec.get("detail") or None})
    return out


TABLE_DESC = {
    "datasets": "공개 데이터셋 1행", "subjects": "대상자 (원본 키 → PatientID)", "studies": "대상자별 검사",
    "series": "검사별 시리즈", "dataset_series": "데이터셋과 시리즈의 소속", "ingest_runs": "이 등록 실행 1행",
    "instances": "DICOM 파일 (SOP Instance)", "transfer_results": "SOP별 전송 결과",
}
# Every column registration writes, in the order of build_records; the DB fills AUTO_COLUMNS itself.
COLUMN_DESC = {
    "datasets": {
        "dataset_uuid": "데이터셋 UUID (원본 폴더에서 결정)", "name": "데이터셋 이름", "source_url": "출처 URL",
        "license": "라이선스", "commercial_use": "상업적 이용 Y/N/U", "description": "설명",
        "patient_id_prefix": "PatientID 접두어"},
    "subjects": {
        "subject_uuid": "dataset UUID + 대상자 키로 결정적 생성", "dataset_uuid": "→ datasets",
        "source_subject_key": "원본 파일명에서 뽑은 대상자 키", "patient_id": "DICOM PatientID (접두어_대상자 키)"},
    "studies": {
        "study_uid": "DICOM StudyInstanceUID", "subject_uuid": "→ subjects", "study_date": "StudyDate (원본에 있을 때)",
        "description": "StudyDescription", "metadata_json": "원본 DICOM의 검사 태그 (DICOM 원본일 때)"},
    "series": {
        "series_uid": "DICOM SeriesInstanceUID", "study_uid": "→ studies", "modality": "CT / MR / RTSTRUCT",
        "description": "SeriesDescription", "metadata_json": "원본에서 뽑은 값: 분할, 크기, 간격, 축 방향, 자료형, 라벨 대상",
        "series_number": "SeriesNumber", "instance_count": "DICOM 파일 수", "source_kind": "원본 형식 nifti / rtstruct / dicom",
        "modality_basis": "modality를 정한 근거", "needs_review": "검토 필요 1/0", "first_run_uuid": "처음 등록한 실행"},
    "dataset_series": {"dataset_uuid": "→ datasets", "series_uid": "→ series"},
    "ingest_runs": {
        "run_uuid": "실행 UUID", "run_label": "실행 ID (run 폴더 이름)", "dataset_uuid": "→ datasets",
        "revision": "plan revision", "manifest_sha256": "전체 변환 manifest 해시 (변환 뒤)", "status": "실행 상태",
        "plan_json": "plan 요약 (규칙·옵션·시리즈별 modality)", "approval_json": "최종 승인 기록",
        "input_root": "원본 경로"},
    "instances": {
        "sop_uid": "DICOM SOPInstanceUID", "series_uid": "→ series", "sop_class_uid": "SOPClassUID",
        "artifact_sha256": "변환된 DICOM 파일 해시", "artifact_relative_path": "run 폴더 기준 파일 경로",
        "prepared_run_uuid": "→ ingest_runs (처음 변환한 실행)", "instance_number": "InstanceNumber",
        "size_bytes": "파일 크기", "transfer_syntax_uid": "TransferSyntaxUID", "source_relative_path": "원본 파일",
        "source_sha256": "원본 파일 해시"},
    "transfer_results": {
        "run_uuid": "→ ingest_runs", "target_key": "전송 대상 AE@host:port", "sop_uid": "→ instances",
        "target_snapshot": "전송 당시 대상 설정", "status": "accepted / warning / failed", "attempts": "시도 횟수",
        "response_code": "응답 코드", "verification_json": "수신 확인 결과", "error_message": "오류"},
}
AUTO_COLUMNS = {"datasets": ["created_at", "updated_at"], "subjects": ["created_at"], "studies": ["created_at"],
                "series": ["created_at", "updated_at"], "dataset_series": [],
                "ingest_runs": ["created_at", "updated_at", "registered_at"], "instances": ["created_at"],
                "transfer_results": ["updated_at"]}


JSON_COLUMNS = {"metadata_json", "plan_json", "approval_json", "target_snapshot", "verification_json"}


def to_db_value(column: str, value):
    if column in JSON_COLUMNS and value is not None:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value
