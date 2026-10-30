import io
import json
import re

import pytest
from conftest import DATASET
from fastapi.testclient import TestClient
from PIL import Image
from test_pipeline import make_run

from dataops import db as dbmod
from dataops.local_send import send_local
from dataops.prepare import prepare
from dataops.web.app import create_app

XSS = "<script>alert(1)</script>"


@pytest.fixture
def prepared(settings, mixed, scp):
    run = make_run(settings, mixed, dict(DATASET, name=XSS, description=XSS))
    prepare(run)
    send_local(run, scp.target())
    return run


def test_pages_render_without_db_and_escape(settings, prepared):
    client = TestClient(create_app(settings))
    r = client.get("/")
    assert r.status_code == 200 and "연결 안 됨" in r.text and prepared.id in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    r = client.get(f"/runs/{prepared.id}")
    assert r.status_code == 200
    assert XSS not in r.text and "&lt;script&gt;" in r.text
    assert "accepted" in r.text                            # the local SCP transfer
    for tab in ("tab-source", "tab-db", "tab-dicom", 'action="/runs/%s/pilot"' % prepared.id):
        assert tab in r.text
    assert client.get(f"/runs/{prepared.id}/records.json").json()["db"] == "DB 설정 없음"
    for page in ("/datasets", "/db", "/backups", "/healthz"):
        assert client.get(page).status_code == 200
    r = client.get(f"/runs/{prepared.id}/report")          # legacy plan: DICOM pass-through + NIfTI
    assert r.status_code == 200 and XSS not in r.text and "&lt;script&gt;" in r.text
    rows = client.get(f"/runs/{prepared.id}/series.json").json()["rows"]
    assert {r["_kind"] for r in rows} >= {"dicom", "nifti"}
    r = client.get("/reports")
    assert r.status_code == 200 and XSS not in r.text


def test_series_viewer_and_png(settings, prepared):
    client = TestClient(create_app(settings))
    plan = prepared.load("plan.json")
    manifest = prepared.load("manifest.json")
    for s in plan["series"]:
        r = client.get(f"/runs/{prepared.id}/series/{s['series_uid']}")
        assert r.status_code == 200 and 'id="viewer"' in r.text and "OS_LJM이 받은 파일을 다시 가져온 것이 아니며" in r.text
    sop = manifest["instances"][0]["sop_uid"]
    r = client.get(f"/runs/{prepared.id}/instances/{sop}.png?wc=40&ww=400")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (32, 32)


def test_slices_sorted_by_position(settings, prepared):
    client = TestClient(create_app(settings))
    s = next(s for s in prepared.load("plan.json")["series"] if s["kind"] == "dicom")
    r = client.get(f"/runs/{prepared.id}/series/{s['series_uid']}")
    sops = r.text.split('data-sops="')[1].split('"')[0].split(",")
    manifest = {i["sop_uid"]: i for i in prepared.load("manifest.json")["instances"]}
    assert [manifest[x]["instance_number"] for x in sops] == sorted(manifest[x]["instance_number"] for x in sops)


@pytest.mark.parametrize("url", ["/runs/..%2F..%2Fetc", "/runs/20260101T000000-deadbeef",
                                 "/db/mysql.user", "/datasets/not-a-uuid"])
def test_unknown_or_traversal_404(settings, prepared, url):
    assert TestClient(create_app(settings)).get(url).status_code == 404


def test_instance_must_belong_to_run_manifest(settings, prepared):
    client = TestClient(create_app(settings))
    assert client.get(f"/runs/{prepared.id}/instances/1.2.3.4.png").status_code == 404
    assert client.get(f"/runs/{prepared.id}/instances/..%2Fmanifest.json.png").status_code == 404


def test_backups_page_reads_manifests(settings):
    d = settings.backup_root / settings.backup_system / "2026-10-28"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"ok": True, "files": [{"path": "db/x.sql.gz", "size": 10,
                                                                         "sha256": "ab" * 32}]}))
    r = TestClient(create_app(settings)).get("/backups")
    assert "2026-10-28" in r.text and "db/x.sql.gz" in r.text


def test_registered_run_compares_equal(settings, prepared, test_db):
    settings.db = test_db
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        dbmod.register_run(conn, prepared)
    client = TestClient(create_app(settings))
    r = client.get(f"/runs/{prepared.id}")
    assert "등록됨" in r.text
    tables = client.get(f"/runs/{prepared.id}/records.json").json()["tables"]
    assert all(set(t["counts"]) <= {"같음"} for t in tables.values())
    r = client.get("/datasets")
    assert DATASET["dataset_uuid"] in r.text
    r = client.get(f"/datasets/{DATASET['dataset_uuid']}?modality=CT")
    assert r.status_code == 200 and "NIfTI→DICOM" in r.text
    assert client.get("/db/instances").status_code == 200


def test_db_browser_follows_links_and_stays_read_only(settings, prepared, test_db):
    settings.db = test_db
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        dbmod.register_run(conn, prepared)
        n_series = dbmod.query(conn, "SELECT COUNT(*) n FROM series")[0]["n"]
        sop = dbmod.query(conn, "SELECT sop_uid, series_uid FROM instances LIMIT 1")[0]
    client = TestClient(create_app(settings))
    r = client.get("/db")
    assert r.status_code == 200 and "데이터 흐름" in r.text and "subjects" in r.text
    duuid = DATASET["dataset_uuid"]
    r = client.get(f"/db/datasets/row?dataset_uuid={duuid}")          # row view: children with counts
    assert r.status_code == 200 and f"/db/subjects?dataset_uuid={duuid}" in r.text
    assert XSS not in r.text and "&lt;script&gt;" in r.text
    r = client.get(f"/db/instances?series_uid={sop['series_uid']}&sort=sop_uid&dir=desc&n=25")
    assert r.status_code == 200 and sop["sop_uid"] in r.text
    assert f"/db/series/row?series_uid={sop['series_uid']}" in r.text      # FK cell links to the parent row
    r = client.get(f"/db/series.csv?q=CT")
    assert r.status_code == 200 and r.text.lstrip("﻿").startswith("series_uid,")
    assert len(client.get("/db/series.csv").text.strip().splitlines()) == n_series + 1
    # names are whitelisted, values bound: injection attempts change nothing and find nothing
    r = client.get("/db/series?modality=CT' OR '1'='1&sort=modality`; DROP TABLE series; --")
    assert r.status_code == 200 and "행 없음" in r.text
    assert client.get("/db/nope").status_code == 404
    assert client.get("/db/series/row?series_uid=1.2.3").status_code == 404
    with dbmod.connection(test_db) as conn:
        assert dbmod.query(conn, "SELECT COUNT(*) n FROM series")[0]["n"] == n_series


def test_db_pages_are_live(settings, prepared, test_db):
    settings.db = test_db
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        dbmod.register_run(conn, prepared)
        uid = dbmod.query(conn, "SELECT series_uid FROM series LIMIT 1")[0]["series_uid"]
    client = TestClient(create_app(settings))
    r = client.get("/db/series")
    sig = r.text.split('data-sig="')[1].split('"')[0]
    assert 'data-tables="series" data-exact="1"' in r.text and "/static/dblive.js" in r.text
    state = lambda: client.get("/db/state.json?t=series,nope&exact=1").json()["marks"]   # noqa: E731
    assert list(state()) == ["series"] and state()["series"] == sig
    with dbmod.connection(test_db) as conn:          # a change within the same second is still seen
        with conn.cursor() as cur:
            cur.execute("UPDATE series SET description=%s WHERE series_uid=%s", ("changed", uid))
        conn.commit()
    assert state()["series"] != sig
    assert "changed" in client.get("/db/series").text
    r = client.get(f"/db/series/row?series_uid={uid}")
    assert 'data-tables="series,dataset_series,deid_series_mappings,instances"' in r.text
    r = client.get("/db")
    assert 'data-exact="0"' in r.text and set(client.get("/db/state.json").json()["marks"]) >= {"series", "instances"}
    # the report's table 7 compares every planned row with the live DB
    rec = client.get(f"/runs/{prepared.id}/records.json").json()
    assert rec["db"] == "ok"
    series = rec["tables"]["series"]
    assert series["counts"] == {"같음": len(series["rows"]) - 1, "변경": 1}
    assert next(x for x in series["rows"] if x["series_uid"] == uid)["_diff"] == "description"
    assert rec["tables"]["instances"]["counts"] == {"같음": len(rec["tables"]["instances"]["rows"])}


def test_db_state_without_db(settings):
    assert TestClient(create_app(settings)).get("/db/state.json").json()["error"] == "DB 설정 없음"


def test_db_browser_without_db(settings):
    r = TestClient(create_app(settings)).get("/db/series")
    assert r.status_code == 200 and "DB 설정 없음" in r.text


def test_sample_view_flags_identifiers_passed_through(settings, mixed):
    """An original DICOM export keeps its other identifiers: they reach the files and the DB."""
    from dataops.compare import sample_view
    from dataops.plan import validate_plan
    run = make_run(settings, mixed, DATASET)
    plan = run.load("plan.json")
    plan["pilot"] = {"subjects": [next(s["subject_key"] for s in plan["series"] if s["kind"] == "dicom")]}
    plan, errors = validate_plan(plan, run.load("inventory.json"))
    run.store_plan(plan, errors, "test")
    prepare(run, "pilot")
    case = sample_view(run)["cases"][0]
    # the PatientID keeps the source ID behind the prefix; the PatientName is replaced by that PatientID
    assert {r["item"].split(" ")[0] for r in case["dicom"][0]["rows"] if r["flag"]} >= {"PatientID", "StudyDate"}
    assert {(t["table"], r["item"]) for t in case["db"] for r in t["rows"] if r["flag"]} >= {
        ("subjects", "source_subject_key"), ("studies", "study_date")}
    page = TestClient(create_app(settings)).get(f"/runs/{run.id}").text
    assert "식별 정보 그대로" in page and "원본 (실제 값)" in page
    assert "DICOM 파일에 들어갈 값" in page and "DB에 들어갈 값" in page and "비식별 켜기" in page   # off by default


def test_deid_rewrites_original_dicom(settings, mixed, tmp_path, monkeypatch):
    """Pseudonymisation on: an original DICOM export leaves with a pseudonym, without names or identifiers and
    with new UIDs that still reference each other; study dates stay."""
    import pydicom
    from dataops import deid
    from dataops.compare import sample_view
    from dataops.inventory import read_dicom_header
    from dataops.plan import validate_plan
    monkeypatch.setenv("DATAOPS_DEID_KEY_FILE", str(tmp_path / "deid.key"))
    files = sorted(p for p in mixed.rglob("*") if p.is_file() and read_dicom_header(p) is not None)
    sops = [str(pydicom.dcmread(p).SOPInstanceUID) for p in files]
    for i, p in enumerate(files):
        ds = pydicom.dcmread(p)
        ds.AccessionNumber, ds.PatientBirthDate, ds.InstitutionName = "ACC123", "19500101", "Some Hospital"
        ds.ReferringPhysicianName = "DOC^ONE"
        ds.add_new(0x00091010, "LO", "private note")
        ref = pydicom.Dataset()
        ref.ReferencedSOPClassUID, ref.ReferencedSOPInstanceUID = ds.SOPClassUID, sops[(i + 1) % len(sops)]
        ds.ReferencedImageSequence = [ref]
        ds.save_as(p)
    run = make_run(settings, mixed, DATASET)
    plan = deid.enable(run.load("plan.json"))
    dicom = next(s for s in plan["series"] if s["kind"] == "dicom")
    source_series, source_pid = dicom["series_uid"], dicom["source_ids"]["patient_id"]
    plan["pilot"] = {"subjects": [dicom["subject_key"]]}
    plan, errors = validate_plan(plan, run.load("inventory.json"))
    assert not errors, errors
    run.store_plan(plan, errors, "test")
    m = prepare(run, "pilot")
    dicom = next(s for s in plan["series"] if s["kind"] == "dicom")
    assert dicom["series_uid"] != source_series and re.fullmatch(rf"{DATASET['patient_id_prefix']}_\d{{4}}",
                                                                  dicom["patient_id"])    # <prefix>_NNNN
    out = [pydicom.dcmread(run.dir / i["path"]) for i in m["instances"] if i["series_uid"] == dicom["series_uid"]]
    assert out and {str(d.SOPInstanceUID) for d in out}.isdisjoint(sops)
    out_sops = {str(d.SOPInstanceUID) for d in out}
    for d in out:
        assert d.PatientID == d.PatientName == dicom["patient_id"] and d.PatientIdentityRemoved == "YES"
        assert d.get("AccessionNumber") == "" and d.get("PatientBirthDate") == "" and d.get("InstitutionName") == ""
        assert d.ReferringPhysicianName == "" and (0x0009, 0x1010) not in d
        assert d.StudyDate == "20200101" and str(d.SeriesInstanceUID) == dicom["series_uid"]
        assert d.file_meta.MediaStorageSOPInstanceUID == d.SOPInstanceUID
        if len(files) == len(out):           # all files in one series: the references are remapped consistently
            assert d.ReferencedImageSequence[0].ReferencedSOPInstanceUID in out_sops
    # the page is for the owner: the left column keeps the real source values, the right one what leaves
    rows = {r["item"].split(" ")[0]: r for r in sample_view(run)["cases"][0]["dicom"][0]["rows"]}
    assert rows["PatientID"]["src"] == source_pid and rows["PatientID"]["out"] == dicom["patient_id"]
    assert rows["AccessionNumber"]["src"] == "ACC123" and not rows["AccessionNumber"]["out"]
    assert rows["StudyDate"]["src"] == rows["StudyDate"]["out"] == "20200101"
    client = TestClient(create_app(settings))
    page = client.get(f"/runs/{run.id}").text
    assert source_pid in page and dicom["patient_id"] in page
    # the series page shows the source identifiers as they are, next to the outgoing ones
    page = client.get(f"/runs/{run.id}/series/{dicom['series_uid']}").text
    assert source_pid in page and source_series in page and dicom["source_ids"]["study_uid"] in page
    assert dicom["patient_id"] in page and dicom["series_uid"] in page


def test_legacy_page_without_and_with_a_snapshot(settings, test_db):
    from dataops import legacy_db
    client = TestClient(create_app(settings))
    r = client.get("/legacy")
    assert r.status_code == 200 and "설정 없음" in r.text and "lab_dataset_items" in r.text and "확인 전" in r.text
    with dbmod.connection(test_db) as conn:                  # stands in for production: read its shape only
        dbmod.migrate(conn)
    cfg = {**test_db, "schemas": (test_db["name"],)}
    snap = legacy_db.refresh(cfg, settings.runtime_dir)
    assert snap["version"] and {t["t"] for t in snap["tables"]} >= {"datasets", "instances"}
    assert any(c["t"] == "instances" and c["c"] == "source_path" for c in snap["columns"])
    r = client.get("/legacy")
    assert r.status_code == 200 and f't-{test_db["name"]}-instances' in r.text and "읽지 않은 schema" in r.text
    with pytest.raises(legacy_db.LegacyError, match="연결 정보"):
        legacy_db.read_schema({"schemas": ("x",)})


def test_legacy_fit_uses_value_limits():
    from dataops.legacy_db import compat
    assert compat("text", "varchar(128)", "datasets.license", "내보내기")[0] == "맞음"           # plan limits it
    assert compat("text", "varchar(512)", "series.source_path", "의미 확인 필요")[0] == "길이 주의"
    assert compat("char(36)", "int(11) NOT NULL", "datasets.dataset_uuid", "external_mappings로 연결")[0] == "ID 대응"
    assert compat("enum('Y','N','U')", "varchar(32)", "datasets.commercial_use", "값 변환 후 내보내기") == (
        "값 변환", "새 값 Y/N/U → 기존 값 형식으로 바꿔야 함")
