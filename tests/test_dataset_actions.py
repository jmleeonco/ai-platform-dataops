"""CONTRACT §102: one row per dataset (same-name list rows grouped), the row buttons 파일럿 보내기 / 로컬 SCP로
보내기 / 샘플 보기 — states and reasons, confirmation, admin-only writes, sends to a stub (loopback) SCP only —
and the bounded sample-preview cache."""
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from dataops import dataset_list as dl
from dataops import preview as pv
from dataops import row_actions as ra
from dataops.jobs import list_jobs, worker
from dataops.local_send import journals
from dataops.runs import Run
from dataops.web import app as appmod
from dataops.web.app import create_app
from test_agent_flow import analyzed, challenge, make_settings  # noqa: F401 - fixture


# --------------------------------------------------------------------------- grouping (no DB)
def _entry(eid, name, modality="CT", folders=(), link=None, avail="not_downloaded", marker=False, row=None):
    return {"entry_id": eid, "name": name, "list_row": row or eid, "kind": "list", "is_marker": marker, "link": link,
            "fields": {"데이터셋 링크": link, "Modality": modality},
            "tags": {"modality": [modality] if modality else [], "body": ["복부"], "roi_category": [], "country": []},
            "eff": {"licence": "CC BY 4.0", "origin": "public", "availability": avail, "commercial_use": "yes"},
            "folders": [{"state": "confirmed", "folder_id": hash(f) % 10**6, "folder": {"path": f, "present": 1,
                                                                                    "role": "source", "size_bytes": None,
                                                                                    "file_count": None}}
                        for f in folders],
            "n_proposed": 0, "availability_note": None, "edited": [], "commercial_reason": None}


def test_same_name_rows_are_one_dataset():
    tcia = "https://www.cancerimagingarchive.net/collection/cptac-ccrcc/"
    entries = [
        _entry(1, "CPTAC-CCRCC", "MR", link=tcia), _entry(2, "CPTAC-CCRCC", "CT", link=tcia),           # per modality
        _entry(3, "CPTAC-PDA-Tumor-Annotations", "CT", folders=["/nas/pda"]),
        _entry(4, "CPTAC-PDA-Tumor-Annotations  ", "PT", folders=["/nas/pda"]),                        # trailing spaces
        _entry(5, "BTCV (신규 없어서 패스)", None, marker=True), _entry(6, "btcv  (신규 없어서 패스)", None, marker=True),
        _entry(7, "Same-Name", "CT", link="https://a.example/x"), _entry(8, "Same-Name", "CT", link="https://b.example/y"),
        _entry(9, "Two-Folders", "CT", folders=["/nas/a"]), _entry(10, "Two-Folders", "MR", folders=["/nas/b"]),
        _entry(11, "Mixed-Stand", "CT", avail="downloaded", link="https://m.example/mixed"),
        _entry(12, "Mixed-Stand", "MR", avail="not_downloaded", link="https://m.example/mixed"),
    ]
    groups = {tuple(e["entry_id"] for e in g) for g in dl.same_name_groups(entries)}
    assert groups == {(1, 2), (3, 4), (5, 6), (11, 12)}           # different link / folder: kept apart
    rows = dl.build(entries, [], [])
    names = [r["name"] for r in rows]
    assert names.count("CPTAC-CCRCC") == 1 and names.count("Same-Name") == 2 and names.count("Two-Folders") == 2
    cc = next(r for r in rows if r["name"] == "CPTAC-CCRCC")
    assert cc["tags"]["modality"] == ["MR", "CT"] and [e["entry_id"] for e in cc["entries"]] == [1, 2]
    pda = next(r for r in rows if r["name"] == "CPTAC-PDA-Tumor-Annotations")    # the name is shown trimmed
    assert len(pda["entries"]) == 2 and len(pda["folders"]) == 1
    mixed = next(r for r in rows if r["name"] == "Mixed-Stand")                  # furthest state, parts named
    assert mixed["stand"] == "downloaded"
    assert [(p["label"], p["stand"]) for p in mixed["stand_parts"]] == [("CT", "downloaded"), ("MR", "not_downloaded")]
    assert len(dl.apply_filters(rows, {"modality": ["CT"]}, "ccrcc")[0]) == 1    # search / filters see the group


# --------------------------------------------------------------------------- buttons
@pytest.fixture
def web(tmp_path, monkeypatch, challenge, scp):  # noqa: F811
    """The web over one list row for the challenge folder (the DB is replaced by a fixed row list)."""
    settings = make_settings(tmp_path, monkeypatch, local_scp=scp.target(), preview_dir=tmp_path / "previews")
    settings.db = {"host": "x", "user": "x", "password": "x", "name": "x"}
    row = dl.build([_entry(1, "SynChallenge", "CT", folders=[str(challenge)])], [], [])[0]
    rows = [row]

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(appmod.dbmod, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(dl.ListCache, "get", lambda self, conn: (rows, [], {"unmatched": [], "totals": None,
                                                                      "datasets": []}, []))
    monkeypatch.setattr(appmod.dbmod, "query", lambda *a, **k: [])
    client = TestClient(create_app(settings))
    client.get("/health")
    return settings, client, rows


def _csrf(client):
    client.get("/new")
    return client.cookies.get("dataops_csrf")


def test_buttons_say_why_and_never_start_an_analysis(web, challenge):  # noqa: F811
    settings, client, rows = web
    a = ra.actions(rows[0], ra.Context(settings, None))
    assert a["samples"]["ok"] and a["samples"]["root"] == str(challenge)
    assert not a["pilot"]["ok"] and "분석" in a["pilot"]["why"] and a["pilot"]["next"][1].startswith("/new?dir=")
    page = client.get("/datasets/row/e1/send/pilot").text
    assert "지금은 할 수 없습니다" in page and "새 분석에서 이 폴더 분석" in page and 'name="confirm"' not in page
    token = _csrf(client)
    r = client.post("/datasets/row/e1/send/pilot", data={"csrf": token, "confirm": "yes", "by": "t"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"] and not list_jobs(settings)          # no analysis, no job
    assert client.get("/datasets/row/e999/send/pilot").status_code == 404
    assert client.get("/datasets/row/x;1/send/pilot").status_code == 404
    hub = ra.actions(rows[0], ra.Context(settings, {"role": "research", "user": "lead"}))
    assert not hub["pilot"]["ok"] and "관리자" in hub["pilot"]["why"] and hub["samples"]["ok"]


def test_pilot_button_sends_to_the_stub_scp_after_confirmation(web, challenge, scp):  # noqa: F811
    settings, client, rows = web
    run = analyzed(settings, challenge)                                          # PILOT_READY, 3 pilot subjects
    a = ra.actions(rows[0], ra.Context(settings, None))
    assert a["pilot"]["ok"] and a["pilot"]["subjects"] == 3 and a["pilot"]["run_id"] == run.id
    assert a["pilot"]["dest"] == scp.target().key and not a["local"]["ok"] and "Central" in a["local"]["why"]
    page = client.get("/datasets/row/e1/send/pilot").text
    assert "3명" in page and scp.target().key in page and "Central" in page and 'name="confirm"' in page
    lst = client.get("/datasets").text
    assert "/datasets/row/e1/send/pilot" in lst and "/datasets/row/e1/samples" in lst
    token = _csrf(client)
    base = {"csrf": token, "confirm": "yes", "run_id": run.id, "subjects": "3", "by": "tester"}
    assert client.post("/datasets/row/e1/send/pilot", data={**base, "csrf": ""}).status_code == 403
    r = client.post("/datasets/row/e1/send/pilot", data={**base, "confirm": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post("/datasets/row/e1/send/pilot", data={**base, "subjects": "2"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and not list_jobs(settings, run.id)[:0]
    r = client.post("/datasets/row/e1/send/pilot", data=base, follow_redirects=False)
    assert r.status_code == 303 and f"/runs/{run.id}?msg=" in r.headers["location"]
    job = list_jobs(settings, run.id)[0]
    assert job["kind"] == "pilot" and job["params"]["target"] == scp.target().key
    worker(settings, once=True)
    assert Run(settings.runs_dir, run.id).meta["status"] == "PILOT_SENT" and scp.received
    assert all(j["target"]["kind"] == "local" for j in journals(run))            # never Central
    busy = ra.actions(rows[0], ra.Context(settings, None))
    assert busy["pilot"]["ok"]                                                   # the run's own images: its slot


def test_local_button_sends_the_whole_dataset_from_a_planned_run(web, challenge, scp):  # noqa: F811
    settings, client, rows = web
    run = analyzed(settings, challenge)
    run.set_status("PLANNED", "test: reviewed, no pilot yet")
    a = ra.actions(rows[0], ra.Context(settings, None))["local"]
    n = len({s["subject_key"] for s in run.load("plan.json")["series"] if not s.get("excluded") and s["kind"] != "rtstruct"})
    assert a["ok"] and a["from_plan"] and a["subjects"] == n
    page = client.get("/datasets/row/e1/send/local").text
    assert f"{n}명" in page and "로컬 전용" in page
    token = _csrf(client)
    r = client.post("/datasets/row/e1/send/local", data={"csrf": token, "confirm": "yes", "run_id": run.id,
                                                          "subjects": str(n), "by": "t"}, follow_redirects=False)
    assert r.status_code == 303
    worker(settings, once=True)
    job = list_jobs(settings, run.id)[0]
    assert job["kind"] == "local" and job["status"] == "done", job.get("error")
    run = Run(settings.runs_dir, run.id)
    assert run.meta["status"] == "LOCAL_SENT" and len(scp.received) == len(run.load("manifest.json")["instances"])
    assert all(j["target"]["kind"] == "local" for j in journals(run))
    # another run holding images blocks a second dataset's send (one slot, §78.6)
    other = dl.build([_entry(2, "Other", "CT", folders=[str(challenge.parent / "other")])], [], [])[0]
    (challenge.parent / "other").mkdir()
    b = ra.actions(other, ra.Context(settings, None))
    assert not b["pilot"]["ok"] and "분석" in b["pilot"]["why"]


# --------------------------------------------------------------------------- samples
def test_samples_render_with_labels_and_stay_bounded(web, challenge, tmp_path):  # noqa: F811
    settings, client, rows = web
    meta = pv.render(settings.preview_dir, str(challenge), internal=False)
    assert meta["state"] == "done" and 1 <= len(meta["subjects"]) <= pv.MAX_SUBJECTS
    first = meta["subjects"][0]["images"][0]
    assert first["label"] == "마스크 1개" and first["slices"] and any(s["labelled"] for s in first["slices"])
    page = client.get("/datasets/row/e1/samples").text
    assert "샘플: SynChallenge" in page and str(challenge) in page
    png = re.search(r'/datasets/row/e1/samples/(s\d_\d_z\d+\.png)', page).group(1)
    r = client.get(f"/datasets/row/e1/samples/{png}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.mode == "RGB" and max(img.size) <= pv.PNG_MAX
    assert client.get("/datasets/row/e1/samples/../../x.png").status_code == 404
    assert client.get("/datasets/row/e1/samples/preview.json").status_code == 404
    # internal data: S1.., no path
    inner = pv.render(tmp_path / "p2", str(challenge), internal=True)
    assert [s["id"] for s in inner["subjects"]][:1] == ["S1"] and inner["root"] is None
    assert all(im["source"] is None for s in inner["subjects"] for im in s["images"])
    # bounded: the oldest dataset's previews go first; purge empties the cache
    (settings.preview_dir / ("0" * 20)).mkdir()
    (settings.preview_dir / ("0" * 20) / "x.png").write_bytes(b"x" * 5000)
    dropped = pv.evict(settings.preview_dir, cap=1000, keep=pv.cache_key(str(challenge)))
    assert "0" * 20 in dropped and (pv.cache_dir(settings.preview_dir, str(challenge)) / "preview.json").is_file()
    assert pv.purge(settings.preview_dir) == 1 and pv.usage(settings.preview_dir)["datasets"] == 0


def test_samples_of_dicom_with_rtstruct(web, challenge, scp, tmp_path):  # noqa: F811
    settings, client, rows = web
    run = analyzed(settings, challenge)          # the extracted samples: DICOM CT / MR series + RTSTRUCT
    meta = pv.render(tmp_path / "p3", str(run.dir / "artifacts"), internal=False)
    ims = [im for s in meta["subjects"] for im in s["images"]]
    assert meta["state"] == "done" and ims and all(im["kind"] == "dicom" for im in ims)
    assert any(im["label"] == "RTSTRUCT" and any(sl["labelled"] for sl in im["slices"]) for im in ims), \
        json.dumps(meta, ensure_ascii=False)[:2000]
