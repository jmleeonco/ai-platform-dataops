"""Standard dataset report: the same numbered tables with the same columns for every dataset; table 6 (DICOM
values per series for the local SCP) checked against the converted files, table 7 (DB rows per table, with the
DB's column names), table 8 (source values) checked against the DB-row projection."""
import csv
import io

import nibabel as nib
from conftest import DATASET
from fastapi.testclient import TestClient
from test_agent_flow import analyzed, challenge, make_settings, piloted  # noqa: F401 - fixtures
from test_pipeline import make_run

from dataops import report
from dataops.metadata import FIELD_KEYS, FIELDS, HEADERS_FILE, TAG_KEYS, build
from dataops.records import COLUMN_DESC, TABLES
from dataops.web.app import _cell, create_app


def shape(rep):
    return [(s["no"], s["title"], s["columns"], [r[0] for r in s["rows"]] if s["no"] == 7 else None)
            for s in rep["sections"] if s["no"] != 10]


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
    assert [s["no"] for s in a["sections"]] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert shape(a) == shape(b)
    assert [r[0] for r in a["sections"][0]["rows"]] == [r[0] for r in b["sections"][0]["rows"]]   # same fields
    assert [r[0] for r in a["sections"][1]["rows"]] == [r[0] for r in b["sections"][1]["rows"]]   # same splits
    assert [r[0] for r in a["sections"][4]["rows"]] == [r[0] for r in b["sections"][4]["rows"]]   # same steps
    for rep in (a, b):
        assert all(set(r) == set(FIELD_KEYS) | {"_uid", "_kind"} for r in rep["series"]["rows"])
        assert [c["key"] for c in rep["dicom"]["columns"]][6:-1] == TAG_KEYS
        assert all(set(r) >= set(TAG_KEYS) for r in rep["dicom"]["rows"])

    assert agent_run.path(HEADERS_FILE).exists()             # extracted by the analysis job
    row = next(r for r in a["series"]["rows"] if r["subject_key"] == "case_0001" and r["_kind"] == "nifti")
    assert "descrip=Time=101010" in row["source_extra"] and "aux_file=Venous_Phase" in row["source_extra"]
    assert row["pilot"] and row["local_status"] is None and row["db_status"] == "미등록"
    assert "DICOM: 없음 · DB: 없음" in next(r for r in a["sections"][8]["rows"] if r[2] == "원본 기타 메타데이터")[5]
    md = build(agent_run)
    assert all(not (f["db_check"] or "").startswith("불일치") for f in md["fields"])
    d = next(r for r in md["dicom"]["rows"] if r["_uid"] == row["_uid"])   # before conversion: plan values
    assert d["origin"].startswith("예정") and d["PatientID"] and d["SOPClassUID"] == "1.2.840.10008.5.1.4.1.1.2"
    assert d["Modality"] == "CT" and d["NumberOfInstances"] > 0 and d["check"] is None
    kinds = [r[1] for r in a["sections"][3]["rows"]]
    assert "오류" not in kinds and "사실" in kinds

    # every destination a source field claims exists: a table-6 column or a table-7 DB column
    for f in FIELDS:
        for tag in filter(None, f.dicom.split(", ")):
            assert tag.split(" ")[0] in TAG_KEYS or "계산" in tag, (f.key, tag)
        if f.db:
            table, col = f.db.split(".")[:2]
            assert col in COLUMN_DESC[table], f.db
    s7 = a["sections"][6]
    assert [r[0] for r in s7["rows"]] == TABLES and s7["rows"][3][2] == len(a["dicom"]["rows"])
    idx = report.dataset_index(settings.runs_dir)
    assert len(idx) == 2 and all(len(r) == len(report.INDEX_COLUMNS) for r in idx)

    client = TestClient(create_app(settings))
    for url in ("/reports", f"/runs/{agent_run.id}", f"/runs/{agent_run.id}/report",
                f"/runs/{legacy_run.id}/report"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "1</span> 데이터셋" in client.get(f"/runs/{agent_run.id}/report").text
    assert 'data-src="/runs/%s/dicom.json"' % agent_run.id in client.get(f"/runs/{agent_run.id}").text
    assert 'data-table="series"' in client.get(f"/runs/{agent_run.id}/report").text
    assert client.get(f"/runs/{agent_run.id}/metadata", follow_redirects=False).status_code == 301
    assert client.get(f"/runs/{agent_run.id}/report.json").json()["schema_version"] == report.SCHEMA_VERSION
    j = client.get(f"/runs/{agent_run.id}/series.json").json()
    assert [c["key"] for c in j["columns"]] == FIELD_KEYS
    lines = list(csv.reader(io.StringIO(client.get(f"/runs/{agent_run.id}/series.csv").text.lstrip("\ufeff"))))
    assert lines[0][0] == "데이터셋" and len(lines[0]) == len(FIELD_KEYS) + 1 and len(lines) == 1 + len(j["rows"])
    j = client.get(f"/runs/{agent_run.id}/dicom.json").json()
    lines = list(csv.reader(io.StringIO(client.get(f"/runs/{agent_run.id}/dicom.csv").text.lstrip("\ufeff"))))
    assert lines[0][7:-1] == TAG_KEYS and len(lines) == 1 + len(j["rows"])
    rec = client.get(f"/runs/{agent_run.id}/records.json").json()
    assert rec["db"] == "DB 설정 없음" and list(rec["tables"]) == TABLES
    for t in TABLES:
        assert [c["key"] for c in rec["tables"][t]["columns"]] == list(COLUMN_DESC[t]) + ["_db", "_diff"]
    assert rec["tables"]["instances"]["note"].startswith("전체 변환 뒤")
    lines = list(csv.reader(io.StringIO(client.get(f"/runs/{agent_run.id}/records/subjects.csv").text.lstrip("\ufeff"))))
    assert lines[0] == list(COLUMN_DESC["subjects"]) + ["DB 상태", "차이 컬럼"]
    assert client.get(f"/runs/{agent_run.id}/records/mysql.csv").status_code == 404
    lines = list(csv.reader(io.StringIO(client.get("/reports.csv").text.lstrip("\ufeff"))))
    assert lines[0] == report.INDEX_COLUMNS and len(lines) == 3


def test_series_checked_against_pilot_headers(tmp_path, monkeypatch, challenge, scp):  # noqa: F811
    settings = make_settings(tmp_path, monkeypatch)
    run = piloted(settings, challenge, scp)
    md = build(run)
    assert {h["modality"] for h in md["headers"]} == {"CT", "MR", "RTSTRUCT"}
    assert md["unknown_tags"] == []                  # every tag the converters write has a stated origin
    tags = {t["key"]: t for t in md["dicom"]["tags"]}
    for key in TAG_KEYS:
        assert not tags[key]["check"].startswith("불일치"), tags[key]
    for key in ("PatientID", "StudyInstanceUID", "SeriesInstanceUID", "Modality", "SOPClassUID", "Rows",
                "PixelSpacing", "SliceThickness", "ImageComments", "ReferencedSeriesInstanceUID",
                "FrameOfReferenceUID", "NumberOfInstances", "SeriesDescription"):
        assert tags[key]["check"].startswith("일치"), tags[key]
    real = [r for r in md["dicom"]["rows"] if r["origin"].startswith("실제 파일")]
    assert real and all(r["pilot"] and r["check"] == "일치" for r in real)
    assert all(r["origin"].startswith("예정") for r in md["dicom"]["rows"] if not r["pilot"])
    rs = next(r for r in real if r["_kind"] == "rtstruct")
    assert rs["ROIName"] and rs["Rows"] is None
    pilot_rows = [r for r in md["rows"] if r["pilot"]]
    assert pilot_rows and all(r["local_status"].startswith("수신") for r in pilot_rows)
    steps = report.build(run, settings)["sections"][4]["rows"]
    assert steps[0][5].startswith("수신") and steps[1][5] == "대기"
    pv = report.db_preview(run)                      # the pilot's files stand in for instances
    assert len(pv["tables"]["instances"]["rows"]) == sum(r["NumberOfInstances"] for r in real)
    assert pv["tables"]["instances"]["note"].startswith("파일럿 파일만")
    assert pv["tables"]["transfer_results"]["rows"][0]["status"] == "accepted"


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
