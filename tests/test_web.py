import io
import json

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
    assert "accepted" in r.text and "DB 설정 없음" in r.text
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
    assert "DB에 없음" in r.text and "등록됨" in r.text
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
    assert 'data-tables="series,dataset_series,instances"' in r.text
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
