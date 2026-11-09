"""Images are transient on this host (CONTRACT §78.6): purge keeps the record, one run holds images at a time,
a published + registered run is purged by its own job, pilot runs only by the owner, the backup holds no image."""
import json
import re
import subprocess

import pytest
from conftest import ROOT, LoopbackSCP
from fastapi.testclient import TestClient
from test_agent_flow import _csrf, analyzed, challenge, make_settings, piloted  # noqa: F401 - fixture

from dataops import retention
from dataops.compare import sample_view
from dataops.config import CentralScp
from dataops.jobs import run_job, start_analysis, submit
from dataops.prepare import prepare, verify_artifacts
from dataops.publish import approve
from dataops.records import build_records
from dataops.runs import Run, RunError
from dataops.synthetic import CHALLENGE_PROPOSAL, make_challenge
from dataops.web.app import create_app

SCRIPT = ROOT / "ops" / "backup" / "nightly_backup.sh"


def test_purge_keeps_the_record_and_is_idempotent(tmp_path, monkeypatch, challenge, scp):
    settings = make_settings(tmp_path, monkeypatch)
    run = piloted(settings, challenge, scp)
    fp = retention.footprint(run.dir)
    assert fp["files"] > 0 and retention.holds_images(run.dir)
    before = sample_view(run, internal=False)
    outgoing = build_records(run, preview=True)["series"]
    kept = {p.name for p in run.dir.iterdir() if p.is_file()}

    r = retention.purge_images(run, "test", "owner")
    assert (r["files"], r["bytes"]) == (fp["files"], fp["bytes"]) and r["by"] == "owner"
    assert not retention.holds_images(run.dir) and not (run.dir / "artifacts").exists()
    assert kept <= {p.name for p in run.dir.iterdir() if p.is_file()}           # nothing of the record went
    assert {"run.json", "plan.json", "manifest-pilot.json", "inventory.json"} <= kept
    assert (run.dir / "transfers").is_dir() and run.meta["status"] == "PILOT_SENT"
    meta = run.meta
    assert meta["images_purged"]["files"] == fp["files"] and meta["images_purged"]["at"].endswith("+00:00")
    assert meta["events"][-1]["event"] == "images_purged"
    # the run page's values survive: outgoing headers from purged_headers.json
    assert build_records(run, preview=True)["series"] == outgoing
    after = sample_view(run, internal=False)
    assert [d["frozen"] for c in after["cases"] for d in c["dicom"]] == \
        [d["frozen"] for c in before["cases"] for d in c["dicom"]]
    assert any("artifact 없음" in p for p in verify_artifacts(run, run.load("manifest-pilot.json")))

    again = retention.purge_images(run, "test")
    assert again["already"] and again["files"] == 0 and len(run.meta["events"]) == len(meta["events"])

    client = TestClient(create_app(settings))
    page = client.get(f"/runs/{run.id}").text
    assert "영상은 Central에 없습니다" in page and "원본 폴더와 plan으로 다시 만들 수 있습니다" in page
    assert "삭제됨" in client.get("/").text
    # regenerable: a new pilot freeze writes the same bytes the manifest recorded
    old = {i["sop_uid"]: i["sha256"] for i in run.load("manifest-pilot.json")["instances"]}
    m = prepare(run, "pilot")
    assert {i["sop_uid"]: i["sha256"] for i in m["instances"]} == old


def test_publish_job_purges_after_central_and_registration(tmp_path, monkeypatch, challenge, scp, test_db):
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
        with pytest.raises(RunError, match="Central 전송이 끝날 때까지"):
            retention.purge_images(run, "test")                 # approved bytes stay until sent
        job = run_job(settings, submit(settings, "publish", run.id, {}, "검토자"))
        assert job["status"] == "done", job["error"]
        meta = run.meta
        assert meta["status"] == "CENTRAL_SENT" and not retention.holds_images(run.dir)
        n = len(run.load("manifest.json")["instances"])
        assert job["result"]["purged"]["files"] >= n and meta["images_purged"]["reason"].startswith("Central")
        assert len(central.received) == n
        for name in ("plan.json", "manifest.json", "registration.json", "purged_headers.json"):
            assert (run.dir / name).is_file(), name
        page = TestClient(create_app(settings)).get(f"/runs/{run.id}").text
        assert "영상은 Central에 있습니다" in page and meta["images_purged"]["at"] in page
    finally:
        central.close()


def test_no_db_registration_keeps_the_images(tmp_path, monkeypatch, challenge, scp):
    central = LoopbackSCP()
    try:
        settings = make_settings(tmp_path, monkeypatch, local_scp=scp.target(),
                                 central=CentralScp(host="127.0.0.1", port=central.port, called_ae="TESTSCP",
                                                    enabled=True))
        run = piloted(settings, challenge, scp)
        approve(run, "검토자", "SynChallenge")
        job = run_job(settings, submit(settings, "publish", run.id, {}, "검토자"))
        assert job["status"] == "done" and run.meta["status"] == "CENTRAL_SENT"
        assert retention.holds_images(run.dir) and "images_purged" not in run.meta     # not registered: kept
    finally:
        central.close()


def test_one_slot_refuses_and_names_the_blocking_run(tmp_path, monkeypatch, challenge, scp, capsys):
    settings = make_settings(tmp_path, monkeypatch, local_scp=scp.target())
    first = piloted(settings, challenge, scp)
    second_root = make_challenge(tmp_path / "public" / "SecondSet")
    second = analyzed(settings, second_root)                   # its sample extraction was refused, not failed
    assert second.meta["status"] == "PLANNED" and not retention.holds_images(second.dir)
    with pytest.raises(RunError, match=re.escape(first.id)) as e:
        prepare(second, "pilot")
    assert "PILOT_SENT" in str(e.value) and f"python -m dataops purge {first.id}" in str(e.value)
    assert retention.holds_images(first.dir)                   # never freed on the system's own initiative
    from dataops import cli
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["prepare", second.id]) == 2                # the CLI says the same (and nothing is written)
    assert first.id in capsys.readouterr().err and second.meta["status"] == "PLANNED"

    client = TestClient(create_app(settings))
    token = _csrf(client)
    r = client.post(f"/runs/{second.id}/pilot", data={"csrf": token}, follow_redirects=False)
    assert "err=" in r.headers["location"] and first.id in r.headers["location"]
    page = client.get(f"/runs/{second.id}").text
    assert f'action="/runs/{first.id}/purge"' in page and "이 서버의 영상 삭제" in page and "data-confirm" in page
    assert "err=" in client.post(f"/runs/{first.id}/purge", data={"csrf": token, "next": second.id},
                                 follow_redirects=False).headers["location"]          # who deletes: required
    r = client.post(f"/runs/{first.id}/purge", data={"csrf": token, "next": second.id, "by": "owner"},
                    follow_redirects=False)
    assert r.headers["location"].startswith(f"/runs/{second.id}?msg=")
    assert not retention.holds_images(first.dir) and first.meta["images_purged"]["by"] == "owner"
    assert prepare(second, "pilot")["instances"]


def test_pilot_runs_are_not_purged_automatically(tmp_path, monkeypatch, challenge, scp):
    settings = make_settings(tmp_path, monkeypatch, local_scp=scp.target())
    run = piloted(settings, challenge, scp)
    other = analyzed(settings, make_challenge(tmp_path / "public" / "Other"))   # a different folder: no supersede
    assert run.meta["status"] == "PILOT_SENT" and retention.holds_images(run.dir)
    assert retention.purge_if_final(run) is None and retention.purge_failed(run) is None
    assert retention.holds_images(run.dir) and other.meta["status"] == "PLANNED"


def test_superseded_and_failed_runs_are_purged(tmp_path, monkeypatch, challenge, scp):
    settings = make_settings(tmp_path, monkeypatch, local_scp=scp.target())
    old = piloted(settings, challenge, scp)
    new = analyzed(settings, challenge)                        # same folder again: the old run is superseded
    assert not retention.holds_images(old.dir) and "대체됨" in old.meta["images_purged"]["reason"]
    assert old.meta["status"] == "PILOT_SENT"                  # its record and status stay
    assert retention.holds_images(new.dir)                     # the new run got the slot for its samples

    bad = {**CHALLENGE_PROPOSAL, "sources": [{"path": "/etc", "role": "image", "split": "train", "note": ""}]}
    pf = tmp_path / "bad.json"
    pf.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("DATAOPS_FAKE_PROPOSAL", str(pf))
    new.set_status("PLANNED", "plan 수정")                    # a re-analysis starts from the review stage
    new.set_status("ANALYZING", "재분석")
    job = run_job(settings, submit(settings, "analyze", new.id, {"input_root": str(challenge)}))
    assert job["status"] == "failed" and new.meta["status"] == "ANALYSIS_FAILED"
    assert not retention.holds_images(new.dir) and new.meta["images_purged"]["reason"].startswith("분석 실패")


def test_cli_runs_shows_the_footprint_and_purge_asks(tmp_path, monkeypatch, challenge, scp, capsys):
    from dataops import cli
    settings = make_settings(tmp_path, monkeypatch)
    run = piloted(settings, challenge, scp)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["runs"]) == 0
    out = capsys.readouterr().out
    assert re.search(rf"{run.id}.*images \d+ MB \(\d+ files\)", out) and "local image footprint" in out
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["purge", run.id]) == 2 and retention.holds_images(run.dir)     # no --yes, no tty: kept
    assert cli.main(["purge", run.id, "--yes"]) == 0 and not retention.holds_images(run.dir)
    cli.main(["runs"])
    assert "images purged" in capsys.readouterr().out


def test_backup_snapshot_excludes_images(tmp_path):
    runs = tmp_path / "runs"
    run = runs / "20261109T000000-abcdef01"
    (run / "artifacts" / "s1").mkdir(parents=True)
    (run / "artifacts" / "s1" / "x.dcm").write_bytes(b"pixels")
    (run / "artifacts" / "s1" / "notes.json").write_text("{}")
    (run / "transfers").mkdir()
    (run / "transfers" / "central.json").write_text("{}")
    (run / "stray.nii.gz").write_bytes(b"pixels")
    for name in ("run.json", "plan.json", "manifest.json"):
        (run / name).write_text("{}")
    dst = tmp_path / "dst"
    listing = subprocess.run(
        ["bash", "-c", 'eval "$(grep "^PIXEL_EXCLUDES=" "$1")"; rsync -an --delete-excluded "${PIXEL_EXCLUDES[@]}" '
                       '--out-format=%n "$2/" "$3/"', "-", str(SCRIPT), str(runs), str(dst)],
        check=True, capture_output=True, text=True).stdout.split()
    files = {p for p in listing if not p.endswith("/")}
    assert files == {f"{run.name}/{n}" for n in ("run.json", "plan.json", "manifest.json", "transfers/central.json")}
    script = SCRIPT.read_text()
    assert '"${PIXEL_EXCLUDES[@]}"' in script and "--exclude='*.nii.gz'" in script


def test_backup_tool_checks_manifests_and_prunes_old_pixels(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("backup_tool", ROOT / "ops/backup/backup_tool.py")
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    from dataops.util import sha256_json
    base = tmp_path / "ai-platform-dataops-files"
    body = {"run_id": "r", "run_uuid": "u", "plan_sha256": "p", "plan_revision": 1, "scope": "full", "instances": []}
    for day in ("2026-11-08", "2026-11-09"):
        r = base / day / "20261109T000000-abcdef01"
        (r / "artifacts" / "s").mkdir(parents=True)
        (r / "manifest.json").write_text(json.dumps({**body, "manifest_sha256": sha256_json(body)}))
        (r / "run.json").write_text("{}")
    (base / "2026-11-08" / "20261109T000000-abcdef01" / "artifacts" / "s" / "a.dcm").write_bytes(b"x" * 100)
    (base / "2026-11-09" / "20261109T000000-abcdef01" / "artifacts" / "s" / "a.dcm").hardlink_to(
        base / "2026-11-08" / "20261109T000000-abcdef01" / "artifacts" / "s" / "a.dcm")
    (base / "not-a-date").mkdir()
    (base / "not-a-date" / "keep.dcm").write_bytes(b"y")
    n, bad = bt.verify_artifacts(base / "2026-11-09")
    assert n == 1 and bad == ["pixel file: 20261109T000000-abcdef01/artifacts/s/a.dcm"]
    assert bt.prune_pixels(base, dry_run=True) == {"links_removed": 2, "bytes_freed": 100, "dry_run": True}
    assert bt.prune_pixels(base, dry_run=False)["bytes_freed"] == 100
    assert bt.verify_artifacts(base / "2026-11-09") == (1, [])
    assert not (base / "2026-11-08" / "20261109T000000-abcdef01" / "artifacts").exists()
    assert (base / "2026-11-08" / "20261109T000000-abcdef01" / "run.json").is_file()
    assert (base / "not-a-date" / "keep.dcm").is_file()                      # only dated snapshots
