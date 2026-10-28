"""Metadata page: every extracted value, where it goes (DICOM / DB / nowhere), checked against the real DB-row
projection and, after the pilot, against the real DICOM headers."""
import csv
import io

import nibabel as nib
from fastapi.testclient import TestClient
from test_agent_flow import analyzed, challenge, make_settings, piloted  # noqa: F401 - fixtures

from dataops.metadata import COLUMNS, HEADERS_FILE, build
from dataops.web.app import _cell, create_app


def test_metadata_before_pilot(tmp_path, monkeypatch, challenge):  # noqa: F811
    img = challenge / "imagesTr" / "case_0001.nii.gz"
    nii = nib.load(str(img))
    nii.header["descrip"] = b"Time=101010"
    nii.header["aux_file"] = b"Venous_Phase"
    nib.save(nii, str(img))
    settings = make_settings(tmp_path, monkeypatch)
    run = analyzed(settings, challenge)
    assert run.path(HEADERS_FILE).exists()           # extracted by the analysis job
    md = build(run)
    assert md["ready"] and len(md["rows"]) == len(run.load("plan.json")["series"])
    row = next(r for r in md["rows"] if r["subject_key"] == "case_0001" and r["kind"] == "nifti")
    assert row["n_descrip"] == "Time=101010" and row["n_aux"] == "Venous_Phase" and row["pilot"]
    flow = {f["key"]: f for f in md["flow"]}
    assert not flow["n_descrip"]["dicom"] and not flow["n_descrip"]["db"]     # stays in the source only
    for f in md["flow"]:
        assert f["db_check"] in (None, "ok"), f
        assert f["dicom_check"] in (None, "pending"), f
    assert flow["patient_id"]["db_checked"] == len(md["rows"])
    assert md["dest"]["local"]["subjects"] == ["case_0001", "case_0501"] and not md["dest"]["local"]["frozen"]
    assert md["dest"]["db"]["series"] == len(md["rows"]) and md["dest"]["db"]["instances"] == 0
    assert md["headers"] == [] and md["unreadable"] == []

    client = TestClient(create_app(settings))
    assert "메타데이터 표" in client.get(f"/runs/{run.id}").text
    page = client.get(f"/runs/{run.id}/metadata")
    assert page.status_code == 200 and "한눈에" in page.text and "원본에만" in page.text
    j = client.get(f"/runs/{run.id}/metadata.json").json()
    assert {c["key"] for c in j["columns"]} == {c.key for c in COLUMNS} and len(j["rows"]) == len(md["rows"])
    lines = list(csv.reader(io.StringIO(client.get(f"/runs/{run.id}/metadata.csv").text.lstrip("﻿"))))
    assert lines[0][0] == "포함" and len(lines) == 3 + len(md["rows"])


def test_metadata_checked_against_pilot_headers(tmp_path, monkeypatch, challenge, scp):  # noqa: F811
    run = piloted(make_settings(tmp_path, monkeypatch), challenge, scp)
    md = build(run)
    assert md["dest"]["local"]["frozen"]
    assert {h["modality"] for h in md["headers"]} == {"CT", "MR", "RTSTRUCT"}
    assert md["unknown_tags"] == []                  # every tag the converters write has a stated origin
    checked = [f for f in md["flow"] if f["dicom_check"] is not None]
    assert checked and all(f["dicom_check"] == "ok" for f in checked), checked
    assert {f["key"] for f in checked} >= {"patient_id", "study_uid", "series_uid", "modality", "spacing_mm",
                                            "shape_xyz", "label_of", "frame_of_reference"}
    assert all(f["db_check"] in (None, "ok") for f in md["flow"])


def test_unreadable_source_is_reported(tmp_path, monkeypatch, challenge):  # noqa: F811
    settings = make_settings(tmp_path, monkeypatch)
    run = analyzed(settings, challenge)
    (challenge / "imagesTr" / "case_0002.nii.gz").unlink()
    assert build(run)["unreadable"] == []            # cached headers until asked to re-read
    assert build(run, refresh=True)["unreadable"] == ["imagesTr/case_0002.nii.gz"]


def test_csv_cells_never_become_formulas():
    assert _cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _cell(-3.5) == "-3.5"
    assert _cell([1, None, "a"]) == "1  a" and _cell(True) == "Y"
