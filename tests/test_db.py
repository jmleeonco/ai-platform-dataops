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


@pytest.fixture
def map_conn(test_map_db):
    c = dbmod.connect(test_map_db)
    dbmod.migrate(c, dbmod.DEIDMAP_MIGRATIONS_DIR)
    yield c
    c.close()


def counts(conn):
    return {k: v for k, v in dbmod.table_counts(conn).items() if k != "schema_migrations"}


def test_migrations_apply_once_and_detect_edits(conn, monkeypatch):
    st = dbmod.migration_status(conn)
    assert st["pending"] == [] and st["current"] == "0005_deid_maps_to_own_db"
    assert dbmod.migrate(conn) == []
    tables = {r["t"] for r in dbmod.query(conn, "SELECT TABLE_NAME t FROM information_schema.TABLES"
                                                 " WHERE TABLE_SCHEMA=DATABASE()")}
    assert not tables & set(dbmod.DEID_TABLES)          # the originals live in the map DB only
    files = dbmod.migration_files()
    monkeypatch.setattr(dbmod, "migration_files",
                        lambda directory=None: [(files[0][0], "0" * 64, files[0][2]), *files[1:]])
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


def test_deid_run_registers_its_maps_and_legacy_columns(conn, map_conn, settings, mixed, tmp_path, monkeypatch):
    """A pseudonymised run: the management DB holds only what leaves; the deid_* maps (oncolabdb enc_* tables),
    in their own map DB, hold every original next to it, so the source can be found from the two DBs."""
    import pydicom
    from dataops import deid
    from dataops.inventory import read_dicom_header
    from dataops.plan import validate_plan
    monkeypatch.setenv("DATAOPS_DEID_KEY_FILE", str(tmp_path / "deid.key"))
    src = {str(pydicom.dcmread(p).SOPInstanceUID): p for p in mixed.rglob("*")
           if p.is_file() and read_dicom_header(p) is not None}
    run = make_run(settings, mixed)
    plan, errors = validate_plan(deid.enable(run.load("plan.json")), run.load("inventory.json"))
    assert not errors
    run.store_plan(plan, errors, "test")
    prepare(run)
    with pytest.raises(dbmod.DbError, match="가명 대응 DB"):         # never a pseudonym without the way back
        dbmod.register_run(conn, run)
    assert counts(conn)["subjects"] == 0
    dbmod.register_run(conn, run, map_conn)
    q = lambda sql: dbmod.query(map_conn if "deid_" in sql else conn, sql)      # noqa: E731
    dicom = [s for s in plan["series"] if s["kind"] == "dicom"]
    subj = {r["original_patient_id"]: r for r in q("SELECT * FROM deid_subject_mappings")}
    assert set(subj) == {s["source_ids"]["patient_id"] for s in dicom} | {
        s["subject_key"] for s in plan["series"] if s["kind"] == "nifti"}
    pids = {r["patient_id"] for r in q("SELECT patient_id FROM subjects")}
    assert {r["pseudonym_patient_id"] for r in subj.values()} == pids and not set(subj) & pids
    ser = {r["series_uid"]: r for r in q("SELECT * FROM deid_series_mappings")}
    for s in dicom:
        assert ser[s["series_uid"]]["original_series_uid"] == s["source_ids"]["series_uid"] != s["series_uid"]
    inst = q("SELECT * FROM deid_instance_mappings")
    assert len(inst) == q("SELECT COUNT(*) n FROM instances")[0]["n"] and any(r["original_sop_uid"] for r in inst)
    for r in inst:
        if r["original_sop_uid"]:                                  # an original DICOM file: found again
            assert src[r["original_sop_uid"]].as_posix() == r["original_source_path"] != r["sop_uid"]
    assert all(r["source_path"].startswith("deid:") for r in q("SELECT source_path FROM instances"))
    run_row = q("SELECT * FROM deid_runs")[0]
    assert run_row["profile"] == deid.PROFILE and run_row["run_uuid"] == run.meta["run_uuid"]
    ct = q("SELECT * FROM series WHERE modality='CT' AND source_kind='dicom' LIMIT 1")[0]
    assert ct["sop_class_uid"] and ct["pixel_spacing"] and ct["slice_thickness"] is not None
    assert ct["frame_of_reference_uid"] is None or ct["frame_of_reference_uid"].startswith("2.25.")
    assert q("SELECT patient_name, patient_id FROM subjects LIMIT 1")[0]["patient_name"] == \
        q("SELECT patient_name, patient_id FROM subjects LIMIT 1")[0]["patient_id"]


def test_public_run_has_no_deid_rows(conn, map_conn, settings, mixed):
    run = make_run(settings, mixed)
    prepare(run)
    dbmod.register_run(conn, run, map_conn)
    for t in dbmod.DEID_TABLES:
        assert dbmod.query(map_conn, f"SELECT COUNT(*) n FROM {t}")[0]["n"] == 0


def test_the_map_is_saved_when_pseudonymised_files_are_made(conn, map_conn, settings, mixed, tmp_path, monkeypatch):
    """The map DB gets original <-> pseudonym as soon as the samples are frozen, before anything is sent or
    registered; saving again changes nothing."""
    from dataops import deid
    from dataops.plan import validate_plan
    monkeypatch.setenv("DATAOPS_DEID_KEY_FILE", str(tmp_path / "deid.key"))
    run = make_run(settings, mixed)
    plan = deid.enable(run.load("plan.json"))
    plan["pilot"] = {"subjects": [plan["series"][0]["subject_key"]]}
    plan, errors = validate_plan(plan, run.load("inventory.json"))
    run.store_plan(plan, errors, "test")
    m = prepare(run, "pilot")
    first = dbmod.save_deid_map(map_conn, run)
    assert first["deid_instance_mappings"]["inserted"] == len(m["instances"]) > 0
    assert first["deid_subject_mappings"]["inserted"] == len({s["subject_key"] for s in plan["series"]})
    again = dbmod.save_deid_map(map_conn, run)
    assert all(v["inserted"] == v["updated"] == 0 for k, v in again.items() if k != "deid_runs")
    assert counts(conn)["subjects"] == 0                    # nothing registered in the management DB yet


def test_every_existing_column_has_a_counterpart_or_a_reason(conn, map_conn):
    """oncolabdb / scpdb (shape only, tests/data): every column of the tables a merge maps value by value is
    filled from a new-DB column or listed in NOT_MAPPED with the reason, and every new-DB column named in the
    mapping exists after the migrations."""
    import json
    import re
    from pathlib import Path
    from dataops import legacy_db
    snap = json.loads((Path(__file__).parent / "data" / "legacy_compared_columns.json").read_text(encoding="utf-8"))
    cov = legacy_db.column_coverage(snap)
    assert len(cov) == len(snap["columns"]) and [c["column"] for c in cov if c["state"] == "대응 없음"] == []
    have = {(r["t"], r["c"]) for c in (conn, map_conn) for r in dbmod.query(
        c, "SELECT TABLE_NAME t, COLUMN_NAME c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()")}
    named = {tuple(m[0].split(" ")[0].split(".")) for m in legacy_db.MAPPING if re.fullmatch(r"\w+\.\w+", m[0].split(" ")[0])}
    assert named and named <= have, named - have
    assert all(m["state"] == "있음" for m in legacy_db.view(snap)["mapping"])
