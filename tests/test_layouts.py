"""Layouts split by file name: binary masks per ROI next to the image (one RTSTRUCT per image), images and labels
in one folder (source patterns); a proposal with nothing to convert; pseudonymisation chosen by location."""
import json

import pydicom
import pytest
from conftest import ROOT

from dataops.config import Settings
from dataops.jobs import run_job, start_analysis
from dataops.prepare import prepare
from dataops.runs import Run
from dataops.synthetic import FLAT_PROPOSAL, MASKS_PROPOSAL, make_masks_and_flat


@pytest.fixture
def data(tmp_path):
    return make_masks_and_flat(tmp_path / "public" / "SynMixed")


def settings_for(tmp_path, monkeypatch, proposal, internal=()):
    pf = tmp_path / f"proposal-{proposal['dataset']['name']}.json"
    pf.write_text(json.dumps(proposal), encoding="utf-8")
    monkeypatch.setenv("DATAOPS_FAKE_PROPOSAL", str(pf))
    return Settings(runtime_dir=tmp_path / "runtime", backup_root=tmp_path / "b", input_roots=(str(tmp_path / "public"),),
                    internal_roots=tuple(internal), agent_cmd=str(ROOT / "tests" / "fake_agent.py"),
                    agent_workdir=tmp_path / "agent")


def analyse(settings, root, deid_on=None) -> Run:
    run, job = start_analysis(settings, str(root), "test", deid_on)
    job = run_job(settings, job)
    assert job["status"] == "done", job["error"]
    return Run(settings.runs_dir, run.id)


def test_binary_masks_become_one_rtstruct_per_image(tmp_path, monkeypatch, data):
    run = analyse(settings_for(tmp_path, monkeypatch, MASKS_PROPOSAL), data)
    plan = run.load("plan.json")
    assert run.load("plan_errors.json") == []
    images = [s for s in plan["series"] if s["kind"] == "nifti"]
    rts = sorted((s for s in plan["series"] if s["kind"] == "rtstruct"), key=lambda s: s["subject_key"])
    assert sorted(s["subject_key"] for s in images) == ["P1", "P2"] and len(rts) == 2
    assert rts[0]["conversion"]["label_names"] == {"1": "CTV", "2": "GTV"}           # sorted by file, overlapping
    assert rts[1]["conversion"]["label_names"] == {"1": "GTV", "2": "empty"} and len(rts[1]["sources"]) == 2
    assert all(r["reference"] == next(i["series_key"] for i in images if i["subject_key"] == r["subject_key"])
               for r in rts)
    m = run.load("manifest-pilot.json") or prepare(run, "pilot")
    by = {s["series_key"]: s for s in m["series"]}
    rep = by[rts[0]["series_key"]]["conversion_report"]
    assert [r["name"] for r in rep["rois"]] == ["CTV", "GTV"] and rep["dice_ok"]
    assert rep["rois"][0]["voxels"] > rep["rois"][1]["voxels"]                      # CTV contains GTV
    rep2 = by[rts[1]["series_key"]]["conversion_report"]
    assert [r["name"] for r in rep2["rois"]] == ["GTV"] and rep2["absent_labels"] == [2]
    inst = next(i for i in m["instances"] if i["series_uid"] == rts[1]["series_uid"])
    assert set(inst["source"]["files"]) == set(rts[1]["sources"])                    # every mask is pinned
    ds = pydicom.dcmread(run.dir / inst["path"])
    assert [str(r.ROIName) for r in ds.StructureSetROISequence] == ["GTV"]


def test_source_patterns_split_one_folder(tmp_path, monkeypatch, data):
    run = analyse(settings_for(tmp_path, monkeypatch, FLAT_PROPOSAL), data)
    plan = run.load("plan.json")
    pairs = sorted((s["subject_key"], s["kind"], s["sources"][0]) for s in plan["series"])
    assert pairs == [("1", "nifti", "flat/volume-1.nii.gz"), ("1", "rtstruct", "flat/labels-1.nii.gz"),
                     ("2", "nifti", "flat/volume-2.nii.gz"), ("2", "rtstruct", "flat/labels-2.nii.gz")]
    left = {(e["path"], e["reason"]) for e in plan["analysis"]["left_out"]}
    assert ("flat", "source 파일 패턴과 맞지 않음") in left                          # notes.txt is accounted for


def test_nothing_to_convert_needs_input_not_failure(tmp_path, monkeypatch, data):
    p = {**FLAT_PROPOSAL, "sources": [{"path": "flat", "role": "ignore", "split": "none", "pattern": "", "note": "x"}],
         "questions": ["영상과 라벨을 어떻게 나눌까요?"]}
    run = analyse(settings_for(tmp_path, monkeypatch, p), data)
    assert run.meta["status"] == "NEEDS_INPUT"
    assert any("영상 시리즈가 없습니다" in e for e in run.load("plan_errors.json"))
    assert run.load("plan.json")["analysis"]["questions"] == ["영상과 라벨을 어떻게 나눌까요?"]


def test_pseudonymisation_follows_location(tmp_path, monkeypatch, data):
    s = settings_for(tmp_path, monkeypatch, MASKS_PROPOSAL, internal=[str(tmp_path / "public" / "SynMixed")])
    assert analyse(s, data).load("plan.json")["deid"]["enabled"] is True          # internal root: on
    assert not (analyse(s, data, deid_on=False).load("plan.json").get("deid") or {}).get("enabled")
    s2 = settings_for(tmp_path, monkeypatch, MASKS_PROPOSAL)
    assert not (analyse(s2, data).load("plan.json").get("deid") or {}).get("enabled")   # public: off


def test_every_file_of_the_folder_is_accounted_for(tmp_path, monkeypatch, data):
    from dataops.publish import coverage
    run = analyse(settings_for(tmp_path, monkeypatch, MASKS_PROPOSAL), data)
    cov = coverage(run)
    on_disk = [p for p in data.rglob("*") if p.is_file()]
    assert cov["files"] == len(on_disk) and cov["bytes"] == sum(p.stat().st_size for p in on_disk)
    assert cov["unclassified"] == 0
    cats = {g["category"]: g["files"] for g in cov["rows"]}
    assert cats["전송"] == 6                                   # 2 CT + 4 masks (one empty: no ROI)
    assert cats["범위 밖: 다른 데이터"] == 5                   # flat/: 4 NIfTI + notes.txt
    plan = run.load("plan.json")                               # a file the plan forgot is caught
    plan["series"][0]["sources"] = []
    assert coverage(run, plan)["unclassified"] == 1
