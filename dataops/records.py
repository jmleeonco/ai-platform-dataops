"""The one projection from a run to DB rows: used both for the HTML preview and for registration."""
from __future__ import annotations

import json
from uuid import UUID, uuid5

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
            "series": [{k: s.get(k) for k in ("series_key", "kind", "excluded", "series_uid", "modality",
                                               "modality_basis", "needs_review", "conversion")}
                       for s in plan["series"]],
            "skipped": len(plan.get("skipped", [])), "unresolved": plan.get("unresolved", [])}


def build_records(run: Run) -> dict:
    meta, plan, manifest = run.meta, run.load("plan.json"), run.load("manifest.json")
    if plan is None or manifest is None:
        raise ValueError("prepare 이후에만 DB 레코드를 만들 수 있습니다")
    from .local_send import journals  # local import: avoids a cycle at module load
    ds = plan["dataset"]
    duuid = ds["dataset_uuid"]
    active = [s for s in plan["series"] if not s.get("excluded")]
    counts = {}
    for inst in manifest["instances"]:
        counts[inst["series_uid"]] = counts.get(inst["series_uid"], 0) + 1
    studies_meta = {st["study_uid"]: st.get("metadata") or {} for st in plan["studies"]}
    subj = {s["subject_key"]: s["patient_id"] for s in active}
    rows = {t: [] for t in TABLES}
    rows["datasets"].append({k: ds.get(k) for k in ("dataset_uuid", "name", "source_url", "license",
                                                    "commercial_use", "description", "patient_id_prefix")})
    for key, pid in sorted(subj.items()):
        rows["subjects"].append({"subject_uuid": subject_uuid(duuid, key), "dataset_uuid": duuid,
                                 "source_subject_key": key, "patient_id": pid})
    seen_studies = set()
    for s in active:
        if s["study_uid"] not in seen_studies:
            seen_studies.add(s["study_uid"])
            m = studies_meta.get(s["study_uid"], {})
            desc = m.get("StudyDescription") or (s["conversion"] or {}).get("study_description")
            rows["studies"].append({"study_uid": s["study_uid"], "subject_uuid": subject_uuid(duuid, s["subject_key"]),
                                    "study_date": m.get("StudyDate"), "description": desc,
                                    "metadata_json": m or None})
        rows["series"].append({"series_uid": s["series_uid"], "study_uid": s["study_uid"], "modality": s["modality"],
                               "description": s.get("description"), "metadata_json": s.get("metadata") or None,
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
    for inst in manifest["instances"]:
        rows["instances"].append({"sop_uid": inst["sop_uid"], "series_uid": inst["series_uid"],
                                  "sop_class_uid": inst["sop_class_uid"], "artifact_sha256": inst["sha256"],
                                  "artifact_relative_path": f"{run.id}/{inst['path']}",
                                  "prepared_run_uuid": meta["run_uuid"], "instance_number": inst["instance_number"],
                                  "size_bytes": inst["size"], "transfer_syntax_uid": inst["transfer_syntax"] or None,
                                  "source_relative_path": inst["source"]["rel"],
                                  "source_sha256": inst["source"]["sha256"]})
    frozen = {i["sop_uid"] for i in manifest["instances"]}
    for j in journals(run):
        for sop, rec in sorted(j["instances"].items()):
            if sop not in frozen:            # e.g. a pilot frozen from an earlier plan revision
                continue
            rows["transfer_results"].append({
                "run_uuid": meta["run_uuid"], "target_key": j["target_key"], "sop_uid": sop,
                "target_snapshot": j["target"], "status": rec["status"], "attempts": rec.get("attempts", 0),
                "response_code": None, "verification_json": None, "error_message": rec.get("detail") or None})
    return rows


JSON_COLUMNS = {"metadata_json", "plan_json", "approval_json", "target_snapshot", "verification_json"}


def to_db_value(column: str, value):
    if column in JSON_COLUMNS and value is not None:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value
