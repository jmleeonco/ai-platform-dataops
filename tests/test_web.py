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
