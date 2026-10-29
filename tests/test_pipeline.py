import json
import shutil
from pathlib import Path

import pytest
from conftest import DATASET, LoopbackSCP

from dataops.config import LocalScp, load_settings
from dataops.inventory import inventory
from dataops.local_send import destination, send_local
from dataops.plan import build_plan, validate_plan
from dataops.prepare import prepare, verify_artifacts
from dataops.records import build_records
from dataops.runs import Run, RunError
from dataops.synthetic import write_ct_series, write_nifti
from dataops.util import sha256_file


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}


def make_run(settings, root, dataset=DATASET, **options) -> Run:
    inv = inventory(root)
    run = Run.create(settings.runs_dir, dataset["dataset_uuid"], str(root))
    plan = build_plan(inv, dataset, run.id, options)
    run.save("inventory.json", inv)
    _, errors = validate_plan(plan, inv)
    run.store_plan(plan, errors, "test")
    return run


def series_by_kind(plan):
    return {s["series_key"].split(":", 1)[1] if s["kind"] == "nifti" else s["patient_id"]: s for s in plan["series"]}


# --------------------------------------------------------------------------- inventory / plan
def test_inventory_mixed_folder_and_backup_not_entered(mixed):
    inv = inventory(mixed)
    assert inv["counts"] == {"dicom": 12, "nifti": 3, "sidecar": 1, "other": 1}
    assert inv["skipped_dirs"] == ["backup"]
    assert not any(f["rel"].startswith("backup/") for f in inv["files"])
    noext = [f for f in inv["files"] if f["rel"].startswith("dicom/P002/")]
    assert all(f["kind"] == "dicom" and "." not in Path(f["rel"]).name for f in noext)
    meta = next(f for f in inv["files"] if f["kind"] == "dicom")["dicom"]["series"]
    assert meta["Modality"] == "CT" and "PatientName" not in meta


def test_plan_groups_and_blocks_masks(settings, mixed):
    run = make_run(settings, mixed)
    plan = run.load("plan.json")
    assert run.meta["status"] == "PLANNED"
    by = series_by_kind(plan)
    assert by["SYN_P001"]["instance_count"] == 6 and by["SYN_P001"]["kind"] == "dicom"
    ct = by["nifti/case01/case01_ct.nii.gz"]
    mr = by["nifti/case02/t1.nii.gz"]
    assert ct["modality"] == "CT" and ct["needs_review"]
    assert mr["modality"] == "MR" and "nifti/case02/t1.json" in mr["sources"]
    assert ct["patient_id"] == "SYNPUB_nifti_case01"
    assert {s["rel"] for s in plan["skipped"]} == {"nifti/case01/case01_mask.nii.gz", "README.txt"}


def test_missing_prefix_is_unresolved_not_localreview(settings, mixed):
    ds = dict(DATASET, patient_id_prefix=None)
    run = make_run(settings, mixed, ds)
    assert run.meta["status"] == "NEEDS_INPUT"
    plan = run.load("plan.json")
    assert any(u["code"] == "patient_id_prefix_missing" for u in plan["unresolved"])
    assert not any("LOCALREVIEW" in (s["patient_id"] or "") for s in plan["series"])
    with pytest.raises(RunError):
        prepare(run)
    # excluding the NIfTI series resolves it
    for s in plan["series"]:
        s["excluded"] = s["kind"] == "nifti"
    plan, errors = validate_plan(plan, run.load("inventory.json"))
    assert errors == [] and plan["unresolved"] == []


def test_conflicting_study_and_duplicate_sop(settings, tmp_path):
    root = tmp_path / "in"
    files = write_ct_series(root / "a", "PX", n=3)
    shutil.copy(files[0], root / "a" / "copy_of_first.dcm")          # identical duplicate: ignored
    run = make_run(settings, root)
    s = run.load("plan.json")["series"][0]
    assert s["instance_count"] == 3 and len(s["duplicates"]) == 1 and run.meta["status"] == "PLANNED"

    import pydicom
    ds = pydicom.dcmread(files[1])
    ds.PixelData = bytes(len(ds.PixelData))                              # same SOP UID, different bytes
    ds.save_as(root / "a" / "tampered.dcm")
    run2 = make_run(settings, root)
    assert run2.meta["status"] == "NEEDS_INPUT"
    assert any("다른 바이트" in e for e in run2.load("plan_errors.json"))


def test_dicom_patient_id_edit_rejected(settings, mixed):
    run = make_run(settings, mixed)
    plan = run.load("plan.json")
    next(s for s in plan["series"] if s["kind"] == "dicom")["patient_id"] = "EDITED"
    _, errors = validate_plan(plan, run.load("inventory.json"))
    assert any("바꿀 수 없습니다" in e for e in errors)


def test_modality_edit_changes_nifti_uids(settings, mixed):
    run = make_run(settings, mixed)
    plan = run.load("plan.json")
    s = next(s for s in plan["series"] if s["kind"] == "nifti" and s["modality"] == "CT")
    before = s["series_uid"]
    s["modality"] = "MR"
    plan, errors = validate_plan(plan, run.load("inventory.json"))
    assert errors == []
    assert next(x for x in plan["series"] if x["series_key"] == s["series_key"])["series_uid"] != before
    run.store_plan(plan, errors, "edit")
    assert run.meta["revision"] == 2


# --------------------------------------------------------------------------- prepare
def test_prepare_freezes_exact_bytes_and_no_input_mutation(settings, mixed):
    before = snapshot(mixed)
    run = make_run(settings, mixed)
    m = prepare(run)
    assert snapshot(mixed) == before
    assert len(m["instances"]) == 6 + 6 + 8 + 8
    for inst in m["instances"]:
        if inst["source"]["kind"] == "dicom":
            assert inst["sha256"] == inst["source"]["sha256"]              # pass-through is byte-identical
    assert verify_artifacts(run) == []
    assert run.meta["status"] == "PREPARED"
    with pytest.raises(RunError):
        run.store_plan(run.load("plan.json"), [], "late edit")


def test_tamper_detection_blocks_send(settings, mixed, scp):
    run = make_run(settings, mixed)
    m = prepare(run)
    victim = run.dir / m["instances"][3]["path"]
    victim.write_bytes(victim.read_bytes()[:-2] + b"\x00\x01")
    assert any("변경됨" in p for p in verify_artifacts(run))
    with pytest.raises(RunError, match="artifact 검증 실패"):
        send_local(run, scp.target())
    assert scp.received == {}


def test_edited_plan_without_validate_is_refused(settings, mixed):
    run = make_run(settings, mixed)
    plan = run.load("plan.json")
    plan["series"][0]["description"] = "edited by hand"
    run.save("plan.json", plan)
    with pytest.raises(RunError, match="validate"):
        prepare(run)


def test_conversion_uids_stable_on_relocation_and_bytes_deterministic(settings, mixed, tmp_path):
    a = make_run(settings, mixed)
    ma = prepare(a)
    moved = tmp_path / "moved" / "elsewhere"
    shutil.copytree(mixed, moved)
    b = make_run(settings, moved)
    mb = prepare(b)
    key = lambda m: sorted((i["sop_uid"], i["sha256"]) for i in m["instances"])  # noqa: E731
    assert key(ma) == key(mb)


def test_changed_pixels_give_new_series_uid(settings, mixed):
    a = make_run(settings, mixed)
    uid_a = series_by_kind(a.load("plan.json"))["nifti/case01/case01_ct.nii.gz"]["series_uid"]
    write_nifti(mixed / "nifti" / "case01" / "case01_ct.nii.gz", "ct", seed=99)
    b = make_run(settings, mixed)
    uid_b = series_by_kind(b.load("plan.json"))["nifti/case01/case01_ct.nii.gz"]["series_uid"]
    assert uid_a != uid_b


def test_input_changed_after_plan_is_refused(settings, mixed):
    run = make_run(settings, mixed)
    write_nifti(mixed / "nifti" / "case02" / "t1.nii.gz", "mr", seed=42)
    with pytest.raises(RunError, match="바뀌었습니다"):
        prepare(run)


# --------------------------------------------------------------------------- local send
def test_send_local_journal_partial_and_retry(settings, mixed):
    run = make_run(settings, mixed)
    m = prepare(run)
    bad = m["instances"][0]["sop_uid"]
    scp = LoopbackSCP(fail_sop_uids={bad})
    try:
        r = send_local(run, scp.target())
        assert r["status"] == "LOCAL_PARTIAL" and r["counts"] == {"accepted": 27, "failed": 1}
        assert set(scp.received) == {i["sop_uid"] for i in m["instances"]} - {bad}
        scp.fail.clear()
        scp.received.clear()
        r2 = send_local(run, scp.target())
        assert r2["status"] == "LOCAL_SENT" and r2["sent_now"] == 1 and set(scp.received) == {bad}
    finally:
        scp.close()
    journal = json.loads(next(run.path("transfers").glob("*.json")).read_text())
    assert journal["instances"][bad]["attempts"] == 2


def test_send_unreachable_scp_marks_failed(settings, mixed):
    run = make_run(settings, mixed)
    prepare(run)
    r = send_local(run, LocalScp("127.0.0.1", 9, "NOBODY", "DATAOPS_TEST"))
    assert r["status"] == "LOCAL_FAILED" and r["counts"] == {"failed": 28}


def test_non_local_target_refused(settings, mixed):
    run = make_run(settings, mixed)
    prepare(run)
    with pytest.raises(RunError, match="로컬 SCP 전용"):
        send_local(run, LocalScp("10.101.1.4", 104, "CENTRAL", "DATAOPS"))
    assert run.path("transfers").exists() is False


def test_review_workstation_must_be_listed():
    ws = LocalScp("10.101.1.107", 4001, "OS_LJM", "DATAOPS", review_hosts=("10.101.1.107",))
    assert destination(ws).host == "10.101.1.107"
    with pytest.raises(RunError, match="로컬 SCP 전용"):
        destination(LocalScp("10.101.1.107", 4001, "OS_LJM", "DATAOPS"))
    with pytest.raises(RunError, match="로컬 SCP 전용"):        # listing one PC does not open the LAN
        destination(LocalScp("10.101.1.4", 104, "OS_LJM", "DATAOPS", review_hosts=("10.101.1.107",)))


def test_central_ae_refused_even_on_loopback():
    with pytest.raises(RunError, match="Central"):
        destination(LocalScp("127.0.0.1", 4001, "oncopacs", "DATAOPS"))


def test_default_target_is_os_ljm_on_review_pc():
    scp = load_settings({}).local_scp
    assert (scp.called_ae, scp.host, scp.port, scp.review_hosts) == ("OS_LJM", "10.101.1.107", 4001, ("10.101.1.107",))
    assert load_settings({"DATAOPS_LOCAL_SCP_REVIEW_HOSTS": ""}).local_scp.review_hosts == ()


def test_concurrent_mutation_is_locked(settings, mixed):
    run = make_run(settings, mixed)
    with run.lock():
        with pytest.raises(RunError, match="사용 중"):
            prepare(run)


def test_records_projection_matches_manifest(settings, mixed, scp):
    run = make_run(settings, mixed)
    m = prepare(run)
    send_local(run, scp.target())
    rec = build_records(run)
    assert len(rec["instances"]) == len(m["instances"]) == len(rec["transfer_results"])
    assert {r["status"] for r in rec["transfer_results"]} == {"accepted"}
    assert sum(s["instance_count"] for s in rec["series"]) == len(m["instances"])
    assert rec["ingest_runs"][0]["manifest_sha256"] == m["manifest_sha256"]
    assert all(r["verification_json"] is None for r in rec["transfer_results"])  # accepted != verified


def test_scan_is_bounded(mixed, monkeypatch):
    from dataops import scan as scanmod
    monkeypatch.setattr(scanmod, "STAT_PER_DIR", 1)
    full = scanmod.scan(mixed)
    assert full["stopped_after_s"] is None and full["totals"]["bytes"] > 0
    assert any(d.get("bytes_estimated") for d in full["dirs"] if d["files"] > 1)
    assert scanmod.scan(mixed, time_budget_s=0)["stopped_after_s"] == 0
