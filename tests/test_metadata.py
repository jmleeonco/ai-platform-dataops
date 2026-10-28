"""Standard dataset report: the same numbered tables with the same columns for every dataset; the series
schema (fixed fields) checked against the DB-row projection and, after the pilot, the real DICOM headers."""
import csv
import io

import nibabel as nib
from conftest import DATASET
from fastapi.testclient import TestClient
from test_agent_flow import analyzed, challenge, make_settings, piloted  # noqa: F401 - fixtures
from test_pipeline import make_run

from dataops import report
from dataops.metadata import FIELD_KEYS, HEADERS_FILE, build
from dataops.web.app import _cell, create_app


def shape(rep):
    return [(s["no"], s["title"], s["columns"]) for s in rep["sections"] if s["no"] != 8]


def test_report_has_the_same_shape_for_every_dataset(tmp_path, monkeypatch, challenge, mixed):  # noqa: F811
    img = challenge / "imagesTr" / "case_0001.nii.gz"
    nii = nib.load(str(img))
    nii.header["descrip"] = b"Time=101010"
    nii.header["aux_file"] = b"Venous_Phase"
    nib.save(nii, str(img))
    settings = make_settings(tmp_path, monkeypatch)
    agent_run = analyzed(settings, challenge)                 # agent proposal, NIfTI + labels
    legacy_run = make_run(settings, mixed, DATASET)          # older CLI plan: DICOM pass-through + NIfTI
    a, b = report.build(agent_run, settings), report.build(legacy_run, settings)
    assert [s["no"] for s in a["sections"]] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert shape(a) == shape(b)
    assert [r[0] for r in a["sections"][0]["rows"]] == [r[0] for r in b["sections"][0]["rows"]]   # same fields
    assert [r[0] for r in a["sections"][1]["rows"]] == [r[0] for r in b["sections"][1]["rows"]]   # same splits
    assert [r[0] for r in a["sections"][4]["rows"]] == [r[0] for r in b["sections"][4]["rows"]]   # same steps
    for rep in (a, b):
        assert all(list(r) == FIELD_KEYS + ["_uid", "_kind"] or set(r) == set(FIELD_KEYS) | {"_uid", "_kind"}
                   for r in rep["series"]["rows"])

    assert agent_run.path(HEADERS_FILE).exists()             # extracted by the analysis job
    row = next(r for r in a["series"]["rows"] if r["subject_key"] == "case_0001" and r["_kind"] == "nifti")
    assert "descrip=Time=101010" in row["source_extra"] and "aux_file=Venous_Phase" in row["source_extra"]
    assert row["pilot"] and row["local_status"] is None and row["db_status"] == "미등록"
    fields = {f["key"]: f for f in a["series"]["fields"]}
    assert not fields["source_extra"]["dicom"] and not fields["source_extra"]["db"]    # stays in the source
    assert all(not (f["db_check"] or "").startswith("불일치") for f in fields.values())
    assert fields["patient_id"]["db_check"] == f"일치 {len(a['series']['rows'])}"
    assert fields["patient_id"]["dicom_check"] == "파일럿 후"
    kinds = [r[1] for r in a["sections"][3]["rows"]]
    assert "오류" not in kinds and "사실" in kinds

    idx = report.dataset_index(settings.runs_dir)
    assert len(idx) == 2 and all(len(r) == len(report.INDEX_COLUMNS) for r in idx)

    client = TestClient(create_app(settings))
    for url in ("/reports", f"/runs/{agent_run.id}", f"/runs/{agent_run.id}/report",
                f"/runs/{legacy_run.id}/report"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "1</span> 데이터셋" in client.get(f"/runs/{agent_run.id}").text
    assert client.get(f"/runs/{agent_run.id}/metadata", follow_redirects=False).status_code == 301
    assert client.get(f"/runs/{agent_run.id}/report.json").json()["schema_version"] == report.SCHEMA_VERSION
    j = client.get(f"/runs/{agent_run.id}/series.json").json()
    assert [f["key"] for f in j["fields"]] == FIELD_KEYS
    lines = list(csv.reader(io.StringIO(client.get(f"/runs/{agent_run.id}/series.csv").text.lstrip("﻿"))))
    assert lines[0][0] == "데이터셋" and len(lines[0]) == len(FIELD_KEYS) + 1 and len(lines) == 1 + len(j["rows"])
    lines = list(csv.reader(io.StringIO(client.get("/reports.csv").text.lstrip("﻿"))))
    assert lines[0] == report.INDEX_COLUMNS and len(lines) == 3


def test_series_checked_against_pilot_headers(tmp_path, monkeypatch, challenge, scp):  # noqa: F811
    settings = make_settings(tmp_path, monkeypatch)
    run = piloted(settings, challenge, scp)
    md = build(run)
    assert {h["modality"] for h in md["headers"]} == {"CT", "MR", "RTSTRUCT"}
    assert md["unknown_tags"] == []                  # every tag the converters write has a stated origin
    fields = {f["key"]: f for f in md["fields"]}
    for key in ("patient_id", "study_uid", "series_uid", "modality", "spacing", "shape", "label_of",
                "frame_of_reference"):
        assert fields[key]["dicom_check"].startswith("일치"), fields[key]
    assert not any((f["dicom_check"] or "").startswith("불일치") for f in md["fields"])
    pilot_rows = [r for r in md["rows"] if r["pilot"]]
    assert pilot_rows and all(r["local_status"].startswith("수신") for r in pilot_rows)
    steps = report.build(run, settings)["sections"][4]["rows"]
    assert steps[0][5].startswith("수신") and steps[1][5] == "대기"


def test_unreadable_source_is_reported(tmp_path, monkeypatch, challenge):  # noqa: F811
    settings = make_settings(tmp_path, monkeypatch)
    run = analyzed(settings, challenge)
    (challenge / "imagesTr" / "case_0002.nii.gz").unlink()
    assert build(run)["unreadable"] == []            # cached headers until asked to re-read
    assert build(run, refresh=True)["unreadable"] == ["imagesTr/case_0002.nii.gz"]
    issues = report.build(run, settings)["sections"][3]["rows"]
    assert issues[0][1] == "오류" and "imagesTr/case_0002.nii.gz" in issues[0][2]
    assert "오류 1" in report.dataset_index(settings.runs_dir)[0][10]


def test_csv_cells_never_become_formulas():
    assert _cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _cell(-3.5) == "-3.5"
    assert _cell([1, None, "a"]) == "1  a" and _cell(True) == "Y"
