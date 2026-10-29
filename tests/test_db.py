"""Registration against a disposable schema on the dev MariaDB (skipped when not configured)."""
import pytest
from test_pipeline import make_run

from dataops import db as dbmod
from dataops.local_send import send_local
from dataops.prepare import prepare
from dataops.records import build_records


@pytest.fixture
def conn(test_db):
    c = dbmod.connect(test_db)
    dbmod.migrate(c)
    yield c
    c.close()


def counts(conn):
    return {k: v for k, v in dbmod.table_counts(conn).items() if k != "schema_migrations"}


def test_migrations_apply_once_and_detect_edits(conn, monkeypatch):
    st = dbmod.migration_status(conn)
    assert st["pending"] == [] and st["current"] == "0003_source_paths"
    assert dbmod.migrate(conn) == []
    files = dbmod.migration_files()
    monkeypatch.setattr(dbmod, "migration_files", lambda: [(files[0][0], "0" * 64, files[0][2]), *files[1:]])
    with pytest.raises(dbmod.DbError, match="바뀌었습니다"):
        dbmod.migrate(conn)


def test_forbidden_schema_names():
    for name in ("scpdb", "oncolabdb", "x;drop"):
        with pytest.raises(dbmod.DbError):
            dbmod.connect({"host": "127.0.0.1", "port": 1, "user": "u", "password": "p", "name": name})


def test_register_is_idempotent(conn, settings, mixed, scp):
    run = make_run(settings, mixed)
    prepare(run)
    send_local(run, scp.target())
    stats = dbmod.register_run(conn, run)
    assert stats["instances"]["inserted"] == 28 and stats["transfer_results"]["inserted"] == 28
    c1 = counts(conn)
    assert c1["series"] == 4 and c1["subjects"] == 4 and c1["dataset_series"] == 4
    stats2 = dbmod.register_run(conn, run)
    assert all(v["inserted"] == 0 for v in stats2.values())
    assert counts(conn) == c1
    row = dbmod.query(conn, "SELECT registered_at, run_label FROM ingest_runs")[0]
    assert row["registered_at"] is not None and row["run_label"] == run.id


def test_same_content_new_run_reuses_identities(conn, settings, mixed):
    a = make_run(settings, mixed)
    prepare(a)
    dbmod.register_run(conn, a)
    b = make_run(settings, mixed)
    prepare(b)
    stats = dbmod.register_run(conn, b)
    assert stats["instances"]["inserted"] == 0 and stats["ingest_runs"]["inserted"] == 1
    first = dbmod.query(conn, "SELECT DISTINCT prepared_run_uuid FROM instances")
    assert [r["prepared_run_uuid"] for r in first] == [a.meta["run_uuid"]]


def test_conflicting_bytes_roll_back_everything(conn, settings, mixed):
    a = make_run(settings, mixed)
    prepare(a)
    dbmod.register_run(conn, a)
    before = counts(conn)
    b = make_run(settings, mixed)
    prepare(b)
    records = build_records(b)
    records["instances"][0]["artifact_sha256"] = "f" * 64
    with pytest.raises(dbmod.RegistrationConflict, match="artifact_sha256"):
        dbmod.register(conn, records)
    assert counts(conn) == before                       # the new ingest_run row was rolled back too


def test_patient_id_conflict_for_same_subject(conn, settings, mixed):
    a = make_run(settings, mixed)
    prepare(a)
    records = build_records(a)
    dbmod.register(conn, records)
    records["subjects"][0]["patient_id"] = "OTHER"
    with pytest.raises(dbmod.RegistrationConflict):
        dbmod.register(conn, records)


def test_series_in_second_dataset_membership(conn, settings, tmp_path):
    from dataops.synthetic import write_ct_series
    root = tmp_path / "only_dicom"
    write_ct_series(root / "x", "SHARED", n=2)
    a = make_run(settings, root)
    prepare(a)
    dbmod.register_run(conn, a)
    rec = build_records(a)
    rec["dataset_series"] = [dict(r, dataset_uuid="22222222-2222-4333-8444-555555555555") for r in rec["dataset_series"]]
    other = dict(rec["datasets"][0], dataset_uuid="22222222-2222-4333-8444-555555555555", name="Second")
    dbmod.register(conn, {**{k: [] for k in rec}, "datasets": [other], "dataset_series": rec["dataset_series"],
                          "ingest_runs": rec["ingest_runs"]})
    rows = dbmod.query(conn, "SELECT dataset_uuid FROM dataset_series ORDER BY dataset_uuid")
    assert len(rows) == 2 and counts(conn)["series"] == 1
