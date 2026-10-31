"""Browser flow: folder -> agent proposal (test double) -> checked plan -> pilot (2 cases, RTSTRUCT) to a
loopback local SCP -> approval -> Central publication (loopback) ; plus every refusal on the way."""
import io
import json
import re

import nibabel as nib
import numpy as np
import pydicom
import pytest
from conftest import ROOT, LoopbackSCP
from fastapi.testclient import TestClient
from PIL import Image

from dataops.compare import sample_view
from dataops.config import CentralScp, Settings
from dataops.jobs import check_input_root, list_jobs, run_job, start_analysis, worker
from dataops.local_send import journals, send_local
from dataops.plan import validate_plan
from dataops.prepare import prepare, verify_artifacts
from dataops.publish import approve, central_ready, publish_central
from dataops.records import build_records
from dataops.runs import Run, RunError
from dataops.spec import SpecError, check_proposal
from dataops.synthetic import CHALLENGE_PROPOSAL, make_challenge
from dataops.web.app import create_app

RS_CLASS = "1.2.840.10008.5.1.4.1.1.481.3"


@pytest.fixture
def challenge(tmp_path):
    return make_challenge(tmp_path / "public" / "SynChallenge")


def make_settings(tmp_path, monkeypatch, proposal=CHALLENGE_PROPOSAL, **kw):
    pf = tmp_path / "proposal.json"
    pf.write_text(json.dumps(proposal), encoding="utf-8")
    monkeypatch.setenv("DATAOPS_FAKE_PROPOSAL", str(pf))
    return Settings(runtime_dir=tmp_path / "runtime", backup_root=tmp_path / "b", input_roots=(str(tmp_path / "public"),),
                    agent_cmd=str(ROOT / "tests" / "fake_agent.py"), agent_workdir=tmp_path / "agent", **kw)


def analyzed(settings, challenge) -> Run:
    run, job = start_analysis(settings, str(challenge), "test")
    job = run_job(settings, job)
    assert job["status"] == "done", job["error"]
    return Run(settings.runs_dir, run.id)


def piloted(settings, challenge, scp) -> Run:
    run = analyzed(settings, challenge)
    prepare(run, "pilot")
    r = send_local(run, scp.target(), scope="pilot")
    assert r["status"] == "PILOT_SENT", r
    return run


def test_analysis_builds_checked_plan(tmp_path, monkeypatch, challenge):
    settings = make_settings(tmp_path, monkeypatch)
    run = analyzed(settings, challenge)
    meta, plan = run.meta, run.load("plan.json")
    assert meta["status"] == "PILOT_READY" and run.load("plan_errors.json") == []   # samples extracted
    assert {x["subject_key"] for x in run.load("manifest-pilot.json")["series"]} == set(
        run.load("plan.json")["pilot"]["subjects"])
    kinds = sorted((s["kind"], s["modality"], s["split"]) for s in plan["series"])
    assert kinds == [("nifti", "CT", "test"), ("nifti", "CT", "train"), ("nifti", "CT", "train"),
                     ("nifti", "MR", "train"), ("rtstruct", "RTSTRUCT", "train"), ("rtstruct", "RTSTRUCT", "train"),
                     ("rtstruct", "RTSTRUCT", "train")]
    mr = next(s for s in plan["series"] if s["subject_key"] == "case_0501" and s["kind"] == "nifti")
    assert "id >= 500" in mr["modality_basis"] and mr["patient_id"] == "SYNCH_ABD_MRCT_PUB_case_0501"
    rt = next(s for s in plan["series"] if s["subject_key"] == "case_0501" and s["kind"] == "rtstruct")
    assert rt["study_uid"] == mr["study_uid"] and rt["patient_id"] == mr["patient_id"]
    left = {e["path"]: e for e in plan["analysis"]["left_out"]}
    assert left["challenge_copy"]["reason"] == "imagesTr의 재정리 사본" and left["challenge_copy"]["files"] == 3
    assert "archive.zip" in left and "readme.md" in left
    assert plan["pilot"]["subjects"] == ["case_0001", "case_0501", "case_0002"]   # agent's two + one with labels
    assert run.load("scan.json")["totals"]["nifti"] == 10
    again = analyzed(settings, challenge)          # the same folder keeps its dataset identity and UIDs
    assert again.meta["dataset_uuid"] == meta["dataset_uuid"]
    assert {s["series_uid"] for s in again.load("plan.json")["series"]} == {s["series_uid"] for s in plan["series"]}


@pytest.mark.parametrize("patch, message", [
    ({"sources": [{"path": "../outside", "role": "image", "split": "train", "note": ""}]}, "상대 경로"),
    ({"sources": [{"path": "nope", "role": "image", "split": "train", "note": ""}]}, "없습니다"),
    ({"sources": [{"path": "labelsTr", "role": "label", "split": "train", "pattern": "a/b", "note": ""}]}, "pattern"),
    ({"label_kind": "masks"}, "label_kind"),
    ({"roi_name_regex": "_RS_.+"}, "그룹 1"),
    ({"subject_pattern": {"regex": "case_\\d+", "note": ""}}, "그룹 1"),
    ({"subject_pattern": {"regex": "(", "note": ""}}, "정규식 오류"),
    ({"dataset": dict(CHALLENGE_PROPOSAL["dataset"], patient_id_prefix="bad prefix!")}, "patient_id_prefix"),
])
def test_proposal_checks_refuse(challenge, patch, message):
    with pytest.raises(SpecError, match=message):
        check_proposal(challenge.resolve(), {**CHALLENGE_PROPOSAL, **patch})


def test_proposal_without_rules_version_is_flagged(challenge):
    """The agent shows it read the dataset-intake skill by echoing codes.json's rules_version."""
    assert not any("dataset-intake" in f for f in check_proposal(challenge, CHALLENGE_PROPOSAL)["findings"])
    stale = dict(CHALLENGE_PROPOSAL, rules_version="")
    assert "dataset-intake" in check_proposal(challenge, stale)["findings"][0]


def test_agent_runs_with_the_intake_skill(tmp_path):
    """The headless agent sees the skill in its work folder (project settings) and may invoke it."""
    from dataops.agent import agent_command, install_skill
    from dataops.rules import SKILL_DIR
    dst = install_skill(tmp_path)
    assert {p.name for p in dst.iterdir()} >= {"SKILL.md", "naming.md", "layouts.md", "vocabulary.md", "codes.json"}
    assert (dst / "codes.json").read_bytes() == (SKILL_DIR / "codes.json").read_bytes()
    cmd = agent_command(Settings(runtime_dir=tmp_path, agent_cmd="claude"), tmp_path, tmp_path)
    assert "Read,Glob,Grep,Skill" in cmd and cmd[cmd.index("--setting-sources") + 1] == "project"


def test_failed_proposal_marks_run_and_job(tmp_path, monkeypatch, challenge):
    bad = {**CHALLENGE_PROPOSAL, "sources": [{"path": "/etc", "role": "image", "split": "train", "note": ""}]}
    settings = make_settings(tmp_path, monkeypatch, bad)
    run, job = start_analysis(settings, str(challenge), "test")
    job = run_job(settings, job)
    assert job["status"] == "failed" and "상대 경로" in job["error"]
    assert Run(settings.runs_dir, run.id).meta["status"] == "ANALYSIS_FAILED"


def test_input_root_must_be_allowed(tmp_path, monkeypatch, challenge):
    settings = make_settings(tmp_path, monkeypatch)
    with pytest.raises(RunError, match="허용된 입력 위치"):
        check_input_root(settings, str(tmp_path))
    (challenge / "backup").mkdir()
    with pytest.raises(RunError, match="backup"):
        check_input_root(settings, str(challenge / "backup"))
    with pytest.raises(RunError, match="절대 경로"):
        check_input_root(settings, "public/SynChallenge")


def test_label_on_another_grid_is_not_converted(tmp_path, monkeypatch, challenge):
    img = nib.load(str(challenge / "labelsTr" / "case_0002.nii.gz"))
    nib.save(nib.Nifti1Image(np.asarray(img.dataobj)[:, :, :6], img.affine), str(challenge / "labelsTr" / "case_0002.nii.gz"))
    run = analyzed(make_settings(tmp_path, monkeypatch), challenge)
    plan = run.load("plan.json")
    assert not any(s["kind"] == "rtstruct" and s["subject_key"] == "case_0002" for s in plan["series"])
    assert any("격자" in s["reason"] for s in plan["skipped"] if s["rel"].endswith("labelsTr/case_0002.nii.gz"))


def test_pilot_sends_samples_with_rtstruct(tmp_path, monkeypatch, challenge, scp):
    run = piloted(make_settings(tmp_path, monkeypatch), challenge, scp)
    m = run.load("manifest-pilot.json")
    assert {s["subject_key"] for s in m["series"]} == {"case_0001", "case_0501", "case_0002"}
    assert run.load("manifest.json") is None
    assert set(scp.received) == {i["sop_uid"] for i in m["instances"]}
    rts = [ds for ds in scp.received.values() if ds.SOPClassUID == RS_CLASS]
    assert len(rts) == 3
    for rs in rts:
        assert {r.ROIName for r in rs.StructureSetROISequence} <= {"liver", "spleen"}
        image_sops = {uid for uid, ds in scp.received.items() if ds.SOPClassUID != RS_CLASS
                      and ds.SeriesInstanceUID == rs.ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0]
                      .RTReferencedSeriesSequence[0].SeriesInstanceUID}
        refs = {c.ContourImageSequence[0].ReferencedSOPInstanceUID for rc in rs.ROIContourSequence
                for c in rc.ContourSequence}
        assert refs and refs <= image_sops
    for s in m["series"]:
        rep = s["conversion_report"]
        if s["kind"] == "rtstruct":
            assert rep["dice_ok"] and rep["min_dice_roundtrip"] >= 0.9
        else:
            assert rep["modality_check"] == "ok"
    assert set(run.load("sources.json")) == {f"{d}/{c}.nii.gz" for d in ("imagesTr", "labelsTr")
                                            for c in ("case_0001", "case_0501", "case_0002")}


def test_approval_gates(tmp_path, monkeypatch, challenge):
    settings = make_settings(tmp_path, monkeypatch)
    run = analyzed(settings, challenge)
    with pytest.raises(RunError, match="파일럿"):
        approve(run, "검토자", "SynChallenge")
    prepare(run, "pilot")
    failing = LoopbackSCP(fail_sop_uids=[run.load("manifest-pilot.json")["instances"][0]["sop_uid"]])
    try:
        assert send_local(run, failing.target(), scope="pilot")["status"] == "PILOT_PARTIAL"
        with pytest.raises(RunError, match="파일럿"):
            approve(run, "검토자", "SynChallenge")
        failing.fail.clear()
        assert send_local(run, failing.target(), scope="pilot")["sent_now"] == 1   # only the missing one
    finally:
        failing.close()
    with pytest.raises(RunError, match="확인 문구"):
        approve(run, "검토자", "wrong name")
    with pytest.raises(RunError, match="승인자"):
        approve(run, "", "SynChallenge")
    a = approve(run, "검토자", "SynChallenge", "OncoStudio에서 확인")
    assert run.meta["status"] == "APPROVED" and a["plan_sha256"] == run.meta["plan_sha256"]
    plan, inv = run.load("plan.json"), run.load("inventory.json")
    with pytest.raises(RunError, match="plan을 바꿀 수 없습니다"):
        run.store_plan(validate_plan(plan, inv)[0], [], "edit after approval")


def test_plan_edit_after_pilot_makes_it_stale(tmp_path, monkeypatch, challenge, scp):
    run = piloted(make_settings(tmp_path, monkeypatch), challenge, scp)
    plan, inv = run.load("plan.json"), run.load("inventory.json")
    plan["dataset"]["license"] = "CC-BY-SA 4.0 (checked)"
    plan, errors = validate_plan(plan, inv)
    run.store_plan(plan, errors, "edit")
    assert run.meta["status"] == "PLANNED"
    with pytest.raises(RunError, match="파일럿"):
        approve(run, "검토자", "SynChallenge")


def test_source_change_after_pilot_is_refused(tmp_path, monkeypatch, challenge, scp):
    settings = make_settings(tmp_path, monkeypatch)
    run = piloted(settings, challenge, scp)
    approve(run, "검토자", "SynChallenge")
    p = challenge / "imagesTr" / "case_0001.nii.gz"
    img = nib.load(str(p))
    nib.save(nib.Nifti1Image(np.asarray(img.dataobj) + np.int16(1), img.affine), str(p))
    with pytest.raises(RunError, match="바뀌었습니다"):
        prepare(run, "full")


def test_central_publication_needs_config_and_approval(tmp_path, monkeypatch, challenge, scp):
    settings = make_settings(tmp_path, monkeypatch)
    run = piloted(settings, challenge, scp)
    assert central_ready(settings.central) == (False, "Central SCP 미설정 (DATAOPS_CENTRAL_HOST/PORT/CALLED_AE)")
    with pytest.raises(RunError, match="미설정"):
        publish_central(run, settings)
    central = LoopbackSCP()
    try:
        settings.central = CentralScp(host="127.0.0.1", port=central.port, called_ae="TESTSCP", enabled=False)
        with pytest.raises(RunError, match="비활성"):
            publish_central(run, settings)
        settings.central.enabled = True
        with pytest.raises(RunError, match="PREPARED"):
            publish_central(run, settings)            # not approved / not frozen
        pilot_sha = {i["sop_uid"]: i["sha256"] for i in run.load("manifest-pilot.json")["instances"]}
        approve(run, "검토자", "SynChallenge")
        m = prepare(run, "full")
        assert all(pilot_sha[i["sop_uid"]] == i["sha256"] for i in m["instances"] if i["sop_uid"] in pilot_sha)
        assert len(pilot_sha) < len(m["instances"])
        r = publish_central(run, settings)
        assert r["status"] == "CENTRAL_SENT" and set(central.received) == {i["sop_uid"] for i in m["instances"]}
        assert publish_central(run, settings)["sent_now"] == 0
        cj = next(j for j in journals(run) if j["target"]["kind"] == "central")
        assert cj["target"]["approved_by"] == "검토자" and cj["scope"] == "full"
        rec = build_records(run)
        assert rec["ingest_runs"][0]["approval_json"]["approved_by"] == "검토자"
        assert {r["source_kind"] for r in rec["series"]} == {"nifti", "rtstruct"}
        sops = {i["sop_uid"] for i in m["instances"]}
        assert all(t["sop_uid"] in sops for t in rec["transfer_results"])
        assert verify_artifacts(run, m) == []
    finally:
        central.close()


def _csrf(client, path="/new"):
    r = client.get(path)
    assert r.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)


def test_browser_flow(tmp_path, monkeypatch, challenge, scp):
    settings = make_settings(tmp_path, monkeypatch, local_scp=scp.target())
    client = TestClient(create_app(settings))
    token = _csrf(client)
    assert client.post("/new", data={"input_root": str(challenge)}, follow_redirects=False).status_code == 403
    r = client.post("/new", data={"input_root": str(challenge), "csrf": token}, follow_redirects=False,
                    headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/new", data={"input_root": "/etc", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and "/new?err=" in r.headers["location"]
    r = client.post("/new", data={"input_root": str(challenge), "csrf": token, "by": "tester"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].split("/runs/")[1].split("?")[0]
    assert client.get(f"/runs/{run_id}/state.json").json()["job"]["kind"] == "analyze"
    worker(settings, once=True)
    page = client.get(f"/runs/{run_id}").text
    assert all(f'id="tab-{t}"' in page for t in ("source", "db", "dicom"))
    report = client.get(f"/runs/{run_id}/report").text
    assert "1</span> 데이터셋" in report and "imagesTr의 재정리 사본" in report
    assert "SYNCH_ABD_MRCT_PUB_case_0001" in {r["PatientID"] for r in client.get(f"/runs/{run_id}/dicom.json").json()["rows"]}
    assert "Ignore previous instructions" not in page + report
    r = client.post(f"/runs/{run_id}/pilot-subjects", data={"csrf": token, "pilot1": "case_0002", "pilot2": "nobody"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post(f"/runs/{run_id}/pilot", data={"csrf": token, "pilot1": "case_0002", "pilot2": "case_0501"},
                    follow_redirects=False)                  # the send button saves the subjects typed beside it
    assert "msg=" in r.headers["location"]
    run = Run(settings.runs_dir, run_id)
    assert run.load("plan.json")["pilot"]["subjects"] == ["case_0002", "case_0501"] and run.meta["revision"] == 2
    assert "err=" in client.post(f"/runs/{run_id}/pilot", data={"csrf": token}, follow_redirects=False).headers["location"]
    worker(settings, once=True)
    assert run.meta["status"] == "PILOT_SENT" and [j["status"] for j in list_jobs(settings, run_id)] == ["done", "done"]
    page = client.get(f"/runs/{run_id}").text
    assert "최종 승인" in page and 'name="approver"' in page
    plan = run.load("plan.json")
    img = next(s for s in plan["series"] if s["subject_key"] == "case_0002" and s["kind"] == "nifti")
    rs = next(s for s in plan["series"] if s["subject_key"] == "case_0002" and s["kind"] == "rtstruct")
    page = client.get(f"/runs/{run_id}/series/{img['series_uid']}").text
    assert "라벨 윤곽" in page and "liver" in page
    m = run.load("manifest-pilot.json")
    rs_sop = next(i["sop_uid"] for i in m["instances"] if i["series_uid"] == rs["series_uid"])
    sop = next(i["sop_uid"] for i in m["instances"] if i["series_uid"] == img["series_uid"] and i["instance_number"] == 4)
    png = client.get(f"/runs/{run_id}/instances/{sop}.png?rs={rs_sop}")
    assert png.status_code == 200 and Image.open(io.BytesIO(png.content)).mode == "RGB"
    assert client.get(f"/runs/{run_id}/instances/{sop}.png?rs={sop}").status_code == 404
    r = client.post(f"/runs/{run_id}/approve", data={"csrf": token, "approver": "검토자", "confirm": "SynChallenge"},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"] and run.meta["status"] == "APPROVED"
    assert list_jobs(settings, run_id)[0]["kind"] == "pilot"       # Central unset: nothing queued
    page = client.get(f"/runs/{run_id}").text
    assert "Central SCP 미설정" in page and "검토자" in page
    r = client.post(f"/runs/{run_id}/publish", data={"csrf": token}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post(f"/runs/{run_id}/dataset", data={"csrf": token, "name": "x", "commercial_use": "Y"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]                       # approved runs are frozen
    for p in ("/", "/jobs", f"/jobs/{list_jobs(settings, run_id)[0]['id']}", "/new?dir=" + str(challenge)):
        assert client.get(p).status_code == 200


def test_publish_job_registers_in_db(tmp_path, monkeypatch, challenge, scp, test_db):
    from dataops import db as dbmod
    central = LoopbackSCP()
    try:
        settings = make_settings(tmp_path, monkeypatch, db=test_db, local_scp=scp.target(),
                                 central=CentralScp(host="127.0.0.1", port=central.port, called_ae="TESTSCP",
                                                    enabled=True))
        with dbmod.connection(test_db) as conn:
            dbmod.migrate(conn)
        run = piloted(settings, challenge, scp)
        approve(run, "검토자", "SynChallenge")
        from dataops.jobs import submit
        job = run_job(settings, submit(settings, "publish", run.id, {}, "검토자"))
        assert job["status"] == "done", job["error"]
        assert run.meta["status"] == "CENTRAL_SENT"
        with dbmod.connection(test_db) as conn:
            counts = dbmod.table_counts(conn)
            kinds = dbmod.query(conn, "SELECT source_kind, modality, COUNT(*) AS n FROM series GROUP BY 1, 2 ORDER BY 1, 2")
            approval = dbmod.query(conn, "SELECT approval_json FROM ingest_runs")[0]["approval_json"]
        assert counts["subjects"] == 4 and counts["series"] == 7
        assert [(k["source_kind"], k["modality"], k["n"]) for k in kinds] == [
            ("nifti", "CT", 3), ("nifti", "MR", 1), ("rtstruct", "RTSTRUCT", 3)]
        assert json.loads(approval)["approved_by"] == "검토자"
        assert counts["transfer_results"] == counts["instances"] + len(run.load("manifest-pilot.json")["instances"])
    finally:
        central.close()


def test_sample_view_shows_real_and_outgoing_values(tmp_path, monkeypatch, challenge):
    settings = make_settings(tmp_path, monkeypatch)
    run = analyzed(settings, challenge)
    view = sample_view(run)
    assert view["extracted"] and len(view["cases"]) == len(run.load("plan.json")["pilot"]["subjects"])
    case = next(c for c in view["cases"] if any(se["kind"] == "nifti" for se in c["dicom"]))
    img = next(se for se in case["dicom"] if se["kind"] == "nifti")
    rows = {r["item"].split(" ")[0]: r for r in img["rows"]}
    key = case["subject_key"]
    assert rows["PatientID"]["src"] == key and rows["PatientID"]["out"] == f"SYNCH_ABD_MRCT_PUB_{key}"
    assert rows["PatientID"]["action"] == "생성" and rows["SeriesInstanceUID"]["action"] == "새 UID 생성"
    assert all(r["out"] for r in img["rows"] if r["action"] == "새 UID 생성")
    db = {}                                                       # the DB part is separate; first row per column
    for t in case["db"]:
        for r in t["rows"]:
            db.setdefault((t["table"], r["item"]), r)
    assert [t["table"] for t in case["db"]][:3] == ["datasets", "subjects", "studies"]
    assert db[("subjects", "source_subject_key")]["action"] == "원본 그대로"
    assert db[("instances", "source_relative_path")]["out"] == img["source"]
    full = db[("instances", "source_path")]["out"]                     # the full path is in the DB too
    assert full.startswith(str(challenge)) and full.endswith(img["source"])
    assert db[("ingest_runs", "input_root")]["out"] == str(challenge)


def test_one_sample_is_enough_to_approve(tmp_path, monkeypatch, challenge, scp):
    run = analyzed(make_settings(tmp_path, monkeypatch), challenge)
    first = run.load("plan.json")["pilot"]["subjects"][0]
    r = send_local(run, scp.target(), scope="pilot", subjects=[first])
    assert r["status"] == "PILOT_SENT" and r["total"] == sum(
        1 for i in run.load("manifest-pilot.json")["instances"]
        if i["series_uid"] in {s["series_uid"] for s in run.load("manifest-pilot.json")["series"]
                               if s["subject_key"] == first})
    assert next(c for c in sample_view(run)["cases"] if c["subject_key"] == first)["sent"].startswith("수신")
    approval = approve(run, "검토자", "SynChallenge")
    assert approval["pilot_subjects"] == [first]


def test_deid_switch_pseudonymises_what_leaves(tmp_path, monkeypatch, challenge):
    from dataops import deid
    from dataops.metadata import build
    monkeypatch.setenv("DATAOPS_DEID_KEY_FILE", str(tmp_path / "deid.key"))
    settings = make_settings(tmp_path, monkeypatch)
    run = analyzed(settings, challenge)
    assert not deid.enabled(run.load("plan.json"))                      # off by default
    plan, errors = validate_plan(deid.enable(run.load("plan.json")), run.load("inventory.json"))
    run.store_plan(plan, errors, "test")
    prepare(run, "pilot")
    view = sample_view(run)
    assert view["deid"] and view["extracted"]
    case = next(c for c in view["cases"] if any(se["kind"] == "nifti" for se in c["dicom"]))
    img = next(se for se in case["dicom"] if se["kind"] == "nifti")
    rows = {r["item"].split(" ")[0]: r for r in img["rows"]}
    pseudo = deid.subject(plan, case["subject_key"])
    assert pseudo != case["subject_key"] and rows["PatientID"]["out"] == f"SYNCH_ABD_MRCT_PUB_{pseudo}"
    assert case["subject_key"] not in rows["PatientName"]["out"] + rows["SeriesDescription"]["out"]
    assert img["source"] not in rows["ImageComments"]["out"]
    db = {}
    for t in case["db"]:
        for r in t["rows"]:
            db.setdefault((t["table"], r["item"]), r)
    assert db[("subjects", "source_subject_key")]["out"] == pseudo
    assert db[("instances", "source_relative_path")]["out"].startswith("deid:")
    assert db[("instances", "source_path")]["out"].startswith("deid:")
    md = build(run)
    assert all(not (f["db_check"] or "").startswith("불일치") for f in md["fields"])
    assert all(r["check"] in (None, "일치") for r in md["dicom"]["rows"])
    again = deid.enable(run.load("plan.json"))                          # same folder, same pseudonyms
    assert deid.subject(again, case["subject_key"]) == pseudo


def test_worker_restart_fails_an_interrupted_analysis(settings):
    """A worker stopped mid-analysis leaves a running job; on restart the job and its run both say failed."""
    from dataops.jobs import submit, worker
    from dataops.runs import Run
    stuck = Run.create(settings.runs_dir, "00000000-0000-5000-8000-000000000001", "/data/a")
    stuck.set_status("ANALYZING", "분석 요청")
    job = submit(settings, "analyze", stuck.id, {"input_root": "/data/a"})
    path = settings.jobs_dir / f"{job['id']}.json"
    path.write_text(json.dumps({**job, "status": "running"}), encoding="utf-8")
    worker(settings, once=True)
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "failed"
    assert Run(settings.runs_dir, stuck.id).meta["status"] == "ANALYSIS_FAILED"
