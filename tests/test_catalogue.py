"""The dataset catalogue (CONTRACT §78.7): list import, licence / commercial use, the read-only folder scan,
availability, hand edits, filters and exports."""
import io
import os
import re

import openpyxl
import pytest
from fastapi.testclient import TestClient

from dataops import catalogue as cat
from dataops import dataset_list as dl
from dataops import db as dbmod
from dataops.web.app import create_app

HEAD = ["데이터셋 이름", "출처", "데이터셋 링크", "ROI category", "Modality", "라이선스 정보", "데이터 수집 나라",
        "촬영 신체 범위", "신규 ROI 리스트", "전체 ROI 리스트", "다운로드 여부", "변환 여부", "검증상태", "검수 일자"]


def _row(name, link, licence, download=None, modality="CT", roi="OAR (liver)", country="미국 (NIH)",
         body="abdomen", new_roi="해당없음", all_roi="liver", verify="검증"):
    return [name, "정리현황(메인)", link, roi, modality, licence, country, body, new_roi, all_roi, download, None,
            verify, "2026-08-20"]


ROWS = [
    _row("02 Pancreas-CT TCIA", "https://tcia/pancreas", "(상업적 이용가능, 출처 표기) CC BY 3.0", "완료",
         roi="OAR (pancreas)", all_roi="pancreas"),
    _row("CHAOS-MR", "https://chaos", "(상업적 이용불가 — 비영리·동일조건) CC BY-NC-SA 4.0", "완료", modality="MR (T2-SPIR)"),
    _row("CMB-MML", "https://tcia/cmb-mml", "(상업적 이용가능, 출처 표기) CC BY 4.0", None, modality="CT"),
    _row("CMB-MML", "https://tcia/cmb-mml", "(상업적 이용가능, 출처 표기) CC BY 4.0", None, modality="PT"),
    _row("Never-Downloaded-Set", "https://example/nd", "(검수 필요) 데이터셋 라이선스 미확인", "X",
         new_roi="mandible, parotid", country="네덜란드"),
    _row("BTCV (신규 없어서 패스)", None, None, None, roi=None, modality=None, country=None, body=None, new_roi=None,
         all_roi=None, verify=None),
    _row("Restricted-Set", "https://example/r", "(접근 제한 — DUA/승인 필요) 챌린지 목적 외 사용 금지, 상업적 이용 불가", "완료"),
]


def write_list(path, rows=ROWS, extra_sheet=False):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "latest"
    ws.append(HEAD)
    for r in rows:
        ws.append(r)
    if extra_sheet:
        wb.create_sheet("other").append(["x"])
    wb.save(path)
    return path


@pytest.fixture
def nas(tmp_path):
    """Synthetic NAS roots: a public dataset folder with files, an empty one, an internal folder, a converted copy."""
    base = tmp_path / "nas"
    pub, onco, conv = base / "Public_dataset", base / "Onco_dataset", base / "processed_Onconii"
    for d in (pub / "02 Pancreas-CT TCIA" / "case1", pub / "03 Combined Healthy Abdominal Organ Segmentation (CHAOS)",
              pub / "Empty-Set", onco / "Inhouse_HNN", conv / "Pancreas-CT", pub / "Restricted-Set"):
        d.mkdir(parents=True)
    (pub / "02 Pancreas-CT TCIA" / "case1" / "a.dcm").write_bytes(b"x" * 100)
    (pub / "02 Pancreas-CT TCIA" / "b.dcm").write_bytes(b"y" * 50)
    (pub / "03 Combined Healthy Abdominal Organ Segmentation (CHAOS)" / "c.nii.gz").write_bytes(b"z")
    (pub / "Restricted-Set" / "r.dcm").write_bytes(b"r")
    (onco / "Inhouse_HNN" / "p.dcm").write_bytes(b"p" * 10)
    (conv / "Pancreas-CT" / "x.nii.gz").write_bytes(b"c")
    (pub / "dataset_URL.txt").write_text("notes")
    roots = ((str(pub), "source", "public"), (str(onco), "source", "internal"), (str(conv), "converted", "public"),
             (str(base / "missing_root"), "source", "public"))
    return base, roots


def _snapshot(root):
    return sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*"))


# --------------------------------------------------------------------------- unit: no DB
@pytest.mark.parametrize("text, licence, cu", [
    ("(상업적 이용가능, 출처 표기) CC BY 3.0", "CC BY 3.0", "yes"),
    ("(상업적 이용가능, 출처 표기) CC BY 4.0 — Data Access 표", "CC BY 4.0", "yes"),
    ("(상업적 이용불가 — 비영리·동일조건) CC BY-NC-SA 4.0", "CC BY-NC-SA 4.0", "no"),
    ("(상업적 이용불가 — 비영리·파생 금지) CC BY-NC-ND 4.0", "CC BY-NC-ND 4.0", "no"),
    ("(상업적 이용가능) CC0 1.0 — Harvard Dataverse", "CC0 1.0", "yes"),
    ("(접근 제한 — DUA/승인 필요) Data Access Agreement 서명, 챌린지 목적 외 사용 제한. 명시적 상업 허용 없음",
     "custom / DUA", "no"),
    ("(접근 제한 — DUA/승인 필요) NIH Controlled Data Access Policy", "custom / DUA", "unknown"),
    ("(검수 필요) 데이터셋 라이선스 미확인 — CC BY-NC-SA 4.0(논문)", "CC BY-NC-SA 4.0 (unverified)", "unknown"),
    ("(상업적 이용가능, 출처 표기) CC BY 4.0 — ⚠기존값 CC BY-NC-SA(비상업)와 상충 (검수 필요)", "CC BY 4.0", "unknown"),
    ("기존값 CC-BY-NC (검수 필요) — 레코드 판독 불가", "CC BY-NC (unverified)", "unknown"),
    ("혼합 — (상업적 이용가능, 출처 표기·동일조건 공유) CC BY-SA 4.0: IXI / (상업적 이용가능, 출처 표기) CC BY 4.0",
     "mixed (CC BY 4.0 / CC BY-SA 4.0)", "yes"),
    (None, "none stated", "unknown"),
])
def test_licence_classification_never_guesses_yes(text, licence, cu):
    got_licence, got_cu, why = cat.classify_licence(text, "public")
    assert (got_licence, got_cu) == (licence, cu) and why


def test_internal_is_commercially_usable():
    assert cat.classify_licence(None, "internal")[:2] == ("internal", "yes")
    assert cat.classify_licence("(상업적 이용불가) CC BY-NC 4.0", "internal")[1] == "yes"


@pytest.mark.parametrize("value, state, path", [
    ("완료", "downloaded", None), ("진행중(부분적재)", "downloading", None), ("미완(차단·동의필요)", "blocked", None),
    ("미완(보류·실패)", "failed", None), ("X", "not_downloaded", None), ("미착수(추출단계)", "not_downloaded", None),
    ("미상", "unknown", None), (None, None, None), ("/mnt/lsh/public_dataset/raw/X", "downloaded", "/mnt/lsh/public_dataset/raw/X"),
])
def test_list_availability(value, state, path):
    assert cat.list_availability(value) == (state, path)


def test_availability_rule_shows_list_vs_scan_difference():
    have = [{"path": "/n/a", "present": 1, "top_entries": 3}]
    assert cat.decide_availability("downloaded", None, have, [])[0] == "downloaded"
    s, note = cat.decide_availability("not_downloaded", None, have, [])
    assert s == "downloaded" and note.startswith("차이:")
    s, note = cat.decide_availability(None, None, have, [])
    assert s == "downloaded" and note.startswith("차이:")
    s, note = cat.decide_availability("downloaded", None, [], [])
    assert s == "unknown" and note.startswith("차이:")
    s, note = cat.decide_availability("downloaded", None, [], have)          # a proposal keeps the list's word
    assert s == "downloaded" and not note.startswith("차이:")
    assert cat.decide_availability(None, None, [], [])[0] == "not_downloaded"
    assert cat.decide_availability("blocked", None, [], [])[0] == "blocked"
    s, note = cat.decide_availability("downloaded", None, [{"path": "/n/e", "present": 1, "top_entries": 0}], [])
    assert s == "unknown" and "비어 있음" in note


def test_name_matching():
    def f(name, role="source"):
        return {"path": "/r/" + name, "name": name, "role": role}
    folders = [f("02 Pancreas-CT TCIA"), f("Decathlon"), f("Decathlon (Task03_Liver)", "converted"), f("TCIA"),
               f("SegRap2023"), f("Adrenal-ACC-Ki67-Seg"), f("09 Multi-Modality Abdominal Multi-Organ Segmentation (AMOS)"),
               f("TCGA-KIRC"), f("tcga_blca")]
    names = {"02 Pancreas-CT TCIA": "a", "Decathlon (Task03_Liver)": "b", "Decathlon (Task02_Heart)": "c",
             "SegRap 2025 Challenge (GTV dataset)": "d", "Adrenal-ACC-Ki67-Seg (Dicom seg format이라 시간 필요)": "e",
             "AMOS 2022 (CT)": "f", "TCGA-BLCA": "g", "TCGA-READ": "h", "Spinal-SEG (TCIA)": "i"}
    got = cat.propose_matches([{"key": k, "name": n} for n, k in names.items()], folders)
    short = {k: [(p.rsplit("/", 1)[1], m) for p, m, _ in v] for k, v in got.items()}
    assert short["a"] == [("02 Pancreas-CT TCIA", "exact")]
    assert ("Decathlon (Task03_Liver)", "exact") in short["b"]
    assert short["c"] == [("Decathlon", "name")]                         # not Task03_Liver
    assert short["d"] == []                                               # another edition
    assert short["e"] == [("Adrenal-ACC-Ki67-Seg", "exact")]              # a Korean note is not part of the name
    assert short["f"] == [("09 Multi-Modality Abdominal Multi-Organ Segmentation (AMOS)", "name")]
    assert short["g"] == [("tcga_blca", "exact")] and short["h"] == []    # no family-prefix proposals
    assert short["i"] == []                                               # "TCIA" alone is not a name


def test_tags():
    t = cat.tags_of({"Modality": "PET/CT, MR (T1)", "ROI category": "GTV, OAR", "촬영 신체 범위": "head and neck",
                     "데이터 수집 나라": "미국, 캐나다", "신규 ROI 리스트": "parotid", "검증상태": "검증"}, "public")
    assert {"CT", "MR", "PT"} <= set(t["modality"]) and {"GTV", "OAR"} <= set(t["roi_category"])
    assert "두경부" in t["body"] and t["country"] == ["미국", "캐나다"] and t["new_rois"] == "yes"
    assert cat.tags_of({"신규 ROI 리스트": "해당없음 (pancreas는 신규 ROI 아님)"}, "public")["new_rois"] == "no"
    assert cat.tags_of({}, "internal")["country"] == ["한국"]


def test_read_list_keys_repeated_rows_and_keeps_every_column(tmp_path):
    path = write_list(tmp_path / "list.xlsx", extra_sheet=True)
    headers, rows = cat.read_list(path)
    assert headers == HEAD and len(rows) == len(ROWS)
    keyed = cat.keyed_rows(rows)
    assert [r["ordinal"] for r in keyed if r["name"] == "CMB-MML"] == [1, 2]
    assert len({r["key"] for r in keyed}) == len(ROWS)
    with pytest.raises(cat.CatalogueError):
        cat.read_list(path, "nope")


def test_list_folders_reads_only(nas):
    root, roots = nas
    before = _snapshot(root)
    folders = cat.list_folders(roots)
    assert _snapshot(root) == before
    paths = {f["name"]: f for f in folders if f.get("path")}
    assert "dataset_URL.txt" not in paths and paths["Empty-Set"]["top_entries"] == 0
    assert paths["02 Pancreas-CT TCIA"]["top_entries"] == 2
    assert any(f.get("error") and f["root"].endswith("missing_root") for f in folders)
    r = cat.walk_size(str(root / "Public_dataset" / "02 Pancreas-CT TCIA"), max_rate=0)
    assert (r["file_count"], r["size_bytes"], r["complete"]) == (2, 150, True)


# --------------------------------------------------------------------------- DB + web
def _import_and_scan(conn, tmp_path, roots, rows=ROWS):
    rep = cat.import_list(conn, write_list(tmp_path / "list.xlsx", rows))
    sc = cat.scan(conn, folders=cat.list_folders(roots))
    return rep, sc


def test_import_scan_availability_and_reimport(settings, test_db, tmp_path, nas):
    root, roots = nas
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        before = _snapshot(root)
        rep, sc = _import_and_scan(conn, tmp_path, roots)
        assert _snapshot(root) == before                                  # the scan wrote nothing
        assert (rep["rows_read"], rep["entries"], rep["markers"], rep["added"]) == (7, 7, 1, 7)
        entries, extra = cat.load(conn)
        by = {(e["name"], e["ordinal"]): e for e in entries}
        p = by[("02 Pancreas-CT TCIA", 1)]
        assert p["eff"]["availability"] == "downloaded" and p["eff"]["commercial_use"] == "yes"
        assert {m["folder"]["path"] for m in p["folders"] if m["state"] == "confirmed"} == \
            {str(root / "Public_dataset" / "02 Pancreas-CT TCIA")}
        chaos = by[("CHAOS-MR", 1)]
        assert chaos["eff"]["commercial_use"] == "no" and chaos["eff"]["licence"] == "CC BY-NC-SA 4.0"
        assert chaos["n_proposed"] == 1 and chaos["eff"]["availability"] == "downloaded"   # proposed, list says done
        assert by[("Never-Downloaded-Set", 1)]["eff"]["availability"] == "not_downloaded"
        assert by[("CMB-MML", 2)]["eff"]["availability"] == "not_downloaded"
        assert by[("BTCV (신규 없어서 패스)", 1)]["is_marker"] == 1
        r = by[("Restricted-Set", 1)]
        assert r["eff"]["commercial_use"] == "no" and r["eff"]["availability"] == "downloaded"
        inh = by[("Inhouse_HNN", 1)]
        assert (inh["kind"], inh["eff"]["origin"], inh["eff"]["commercial_use"], inh["eff"]["availability"]) == \
            ("folder", "internal", "yes", "downloaded")
        assert not (inh["availability_note"] or "").startswith("차이:")
        assert str(root / "Public_dataset" / "Empty-Set") in {f["path"] for f in extra["unmatched"]}

        # filters: commercial use = yes keeps Pancreas, drops CHAOS-MR; counts per value
        rows, counts = cat.apply_filters(entries, {"commercial": ["yes"]})
        names = {e["name"] for e in rows}
        assert "02 Pancreas-CT TCIA" in names and "CHAOS-MR" not in names and "Inhouse_HNN" in names
        assert counts["commercial"]["no"] == 2 and counts["availability"]["downloaded"] >= 2
        rows, _ = cat.apply_filters(entries, {"new_rois": ["yes"]}, "parotid")
        assert [e["name"] for e in rows] == ["Never-Downloaded-Set"]

        # a hand edit is kept over a re-import that changes the same value, and reported
        eid = by[("Never-Downloaded-Set", 1)]["entry_id"]
        with pytest.raises(cat.CatalogueError):
            cat.edit_entry(conn, eid, {"commercial_use": "no"}, "", "lead")
        assert cat.edit_entry(conn, eid, {"commercial_use": "no"}, "license page says NC", "lead") == ["commercial_use"]
        rows2 = [r[:] for r in ROWS if r[0] != "CMB-MML"]
        next(r for r in rows2 if r[0] == "Never-Downloaded-Set")[5] = "(상업적 이용가능, 출처 표기) CC BY 4.0"   # now "yes"
        rows2.append(_row("New-Set", "https://example/new", "(상업적 이용가능) CC0 1.0", None))
        rep2 = cat.import_list(conn, write_list(tmp_path / "list2.xlsx", rows2))
        assert rep2["added"] == 1 and sorted(rep2["gone"]) == ["CMB-MML", "CMB-MML"]
        assert [c["name"] for c in rep2["changed"]] == ["Never-Downloaded-Set"]
        assert rep2["held_hand_edits"] == [{"name": "Never-Downloaded-Set", "field": "commercial_use",
                                            "imported": "yes", "hand_edit": "no"}]
        cat.scan(conn, folders=cat.list_folders(roots))
        entries, _ = cat.load(conn)
        e = next(x for x in entries if x["entry_id"] == eid)
        assert (e["commercial_use"], e["eff"]["commercial_use"]) == ("yes", "no")
        assert "CMB-MML" not in {x["name"] for x in entries}               # gone from the list: flagged, not shown

        # a person confirms the proposed CHAOS folder; the size walk fills sizes
        chaos = next(x for x in entries if x["name"] == "CHAOS-MR")
        fid = chaos["folders"][0]["folder_id"]
        cat.decide_match(conn, chaos["entry_id"], fid, "confirmed", "lead", "evidence: Train_Sets/MR 20 subjects")
        decided = next(x for x in cat.load(conn)[0] if x["entry_id"] == chaos["entry_id"])["folders"]
        assert next(m for m in decided if m["folder_id"] == fid)["note"] == "evidence: Train_Sets/MR 20 subjects"
        res = cat.size_walk(conn, budget_s=60, max_rate=0)
        assert res["walked"] >= 3 and res["left"] == 0
        entries, _ = cat.load(conn)
        p = next(x for x in entries if x["name"] == "02 Pancreas-CT TCIA")
        assert (p["file_count"], p["size_bytes"]) == (2, 150)
        sm = cat.summary(conn)
        assert sm["list_rows"] == 6 and sm["internal_folders"] == 1 and sm["commercial_by_origin"]["internal"] == {"yes": 1}
        assert _snapshot(root) == before


def test_catalogue_pages_filters_exports_and_edits(settings, test_db, tmp_path, nas):
    root, roots = nas
    settings.db = test_db
    settings.input_roots = (str(root / "Public_dataset"),)
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        _import_and_scan(conn, tmp_path, roots)
        entries, _ = cat.load(conn)
    pid = next(e["entry_id"] for e in entries if e["name"] == "02 Pancreas-CT TCIA")
    chaos = next(e["entry_id"] for e in entries if e["name"] == "CHAOS-MR")
    client = TestClient(create_app(settings))
    r = client.get("/catalog", follow_redirects=False)                 # the old page leads to the one list
    assert r.status_code == 302 and r.headers["location"] == "/datasets"
    assert client.get("/catalog?commercial=yes", follow_redirects=False).headers["location"] == \
        "/datasets?commercial=yes"
    r = client.get("/catalog")
    assert r.status_code == 200 and "02 Pancreas-CT TCIA" in r.text and "CHAOS-MR" in r.text and "비상업 제외" in r.text
    assert "상업 불가" in r.text and "받음" in r.text and 'href="/datasets?commercial=yes&amp;commercial=unknown"' in r.text
    r = client.get("/datasets?commercial=yes")
    assert "02 Pancreas-CT TCIA" in r.text and ">CHAOS-MR<" not in r.text
    r = client.get("/datasets?commercial=yes&origin=public&q=pancreas")
    assert re.search(r"<b>1</b> / 7 행", r.text)                  # CMB-MML CT + PT: one row (§102)
    csv_text = client.get("/catalog.csv?commercial=no").text            # follows the redirect
    assert "CHAOS-MR" in csv_text and "Pancreas" not in csv_text and "라이선스 정보" in csv_text
    x = client.get("/datasets.xlsx?availability=downloaded")
    ws = openpyxl.load_workbook(io.BytesIO(x.content)).active
    names = [row[0].value for row in ws.iter_rows(min_row=2, max_col=1)]
    assert "02 Pancreas-CT TCIA" in names and "Never-Downloaded-Set" not in names
    assert ws.max_column == len(dl.EXPORT_FIXED) + len(HEAD)
    r = client.get(f"/catalog/{pid}")
    assert r.status_code == 200 and "/new?dir=" in r.text and "라이선스 정보" in r.text and "(상업적 이용가능" in r.text
    r = client.get(f"/catalog/{chaos}")
    assert "이 폴더가 맞음" in r.text and "/new?dir=" not in r.text
    assert client.get("/catalog/999999").status_code == 404
    # writes need the CSRF token and a note
    assert client.post(f"/catalog/{pid}/edit", data={"commercial_use": "no", "note": "x", "by": "t"}).status_code == 403
    token = client.cookies.get("dataops_csrf")
    r = client.post(f"/catalog/{pid}/edit", data={"csrf": token, "commercial_use": "no", "note": "", "by": "t"},
                    follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = client.post(f"/catalog/{pid}/edit", data={"csrf": token, "commercial_use": "no", "note": "checked", "by": "t"},
                    follow_redirects=True)
    assert "사람이 고친 값" in r.text and "checked" in r.text
    assert ">02 Pancreas-CT TCIA<" not in client.get("/datasets?commercial=yes").text
    r = client.post(f"/catalog/{chaos}/match", data={"csrf": token, "folder_id": "abc", "state": "confirmed", "by": "t"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_catalogue_page_without_db_or_tables(settings, test_db):
    client = TestClient(create_app(settings))
    assert client.get("/datasets").status_code == 200
    settings.db = test_db                                            # schema exists, tables do not
    r = TestClient(create_app(settings)).get("/datasets")
    assert r.status_code == 200 and "db-migrate" in r.text


def test_migration_is_additive():
    for name in ("0008_dataset_catalogue.sql", "0009_catalog_dataset_links.sql"):
        sql = (cat.Path(dbmod.MIGRATIONS_DIR) / name).read_text(encoding="utf-8")
        assert not re.search(r"\b(DROP|ALTER|DELETE|UPDATE|TRUNCATE)\b", sql, re.I)
    sql = (cat.Path(dbmod.MIGRATIONS_DIR) / "0010_catalog_match_note.sql").read_text(encoding="utf-8")
    stmts = [x.strip() for x in re.sub(r"--[^\n]*", "", sql).split(";") if x.strip()]
    assert stmts == ["ALTER TABLE catalog_matches ADD COLUMN note VARCHAR(2000) NULL AFTER decided_at"]
    assert os.path.basename(sorted(cat.Path(dbmod.MIGRATIONS_DIR).glob("0*.sql"))[-1]) == \
        "0010_catalog_match_note.sql"


# --------------------------------------------------------------------------- §78.8 one dataset list
def _register(cur, uuid, name, *, prefix=None, commercial="U", legacy_id=None, input_root=None, central=0):
    """A registered dataset as the importers / the intake leave it: one subject, study, CT series, an intake run
    reading `input_root` and `central` instances accepted by the central target."""
    cur.execute("INSERT INTO datasets (dataset_uuid, name, license, commercial_use, patient_id_prefix)"
                " VALUES (%s,%s,%s,%s,%s)", (uuid, name, "CC BY 4.0" if commercial == "Y" else None, commercial, prefix))
    if legacy_id:
        cur.execute("INSERT INTO external_mappings VALUES (%s,'oncolabdb.lab_datasets',%s)", (uuid, legacy_id))
    sub, st = uuid[:8] + "-0000-4000-8000-000000000000", "1.2." + uuid[0]
    se = st + ".1"
    cur.execute("INSERT INTO subjects (subject_uuid, dataset_uuid, source_subject_key, patient_id)"
                " VALUES (%s,%s,'k','P1')", (sub, uuid))
    cur.execute("INSERT INTO studies (study_uid, subject_uuid) VALUES (%s,%s)", (st, sub))
    cur.execute("INSERT INTO series (series_uid, study_uid, modality, instance_count) VALUES (%s,%s,'CT',3)", (se, st))
    cur.execute("INSERT INTO dataset_series (dataset_uuid, series_uid) VALUES (%s,%s)", (uuid, se))
    if input_root:
        run = uuid[:8] + "-1111-4000-8000-000000000000"
        cur.execute("INSERT INTO ingest_runs (run_uuid, run_label, dataset_uuid, revision, status, plan_json,"
                    " input_root, registered_at) VALUES (%s,%s,%s,1,'CENTRAL_SENT','{}',%s,CURRENT_TIMESTAMP)",
                    (run, "r-" + uuid[:6], uuid, input_root))
        for i in range(central):
            sop = f"{se}.{i}"
            cur.execute("INSERT INTO instances (sop_uid, series_uid, sop_class_uid, artifact_sha256,"
                        " artifact_relative_path, prepared_run_uuid) VALUES (%s,%s,'1.2.840',%s,'a',%s)",
                        (sop, se, "0" * 64, run))
            cur.execute("INSERT INTO transfer_results (run_uuid, target_key, sop_uid, target_snapshot, status)"
                        " VALUES (%s,'central:ONCOPACS@x:4002',%s,'{}','accepted')", (run, sop))


U_PAN, U_CHAOS, U_DEP, U_A, U_B = (f"{i}0000000-aaaa-4bbb-8ccc-00000000000{i}" for i in range(1, 6))


def test_one_dataset_list_merges_only_on_links(settings, test_db, tmp_path, nas):
    root, roots = nas
    settings.db = test_db
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        _import_and_scan(conn, tmp_path, roots)
        with conn.cursor() as cur:
            # the intake read the Pancreas folder and sent it to central
            _register(cur, U_PAN, "Pancreas-CT (intake)", prefix="PANC_ABD_CT_PUB", commercial="Y",
                      input_root=str(root / "Public_dataset" / "02 Pancreas-CT TCIA"), central=2)
            _register(cur, U_CHAOS, "CHAOS", legacy_id=71, commercial="N")          # similar name, no link
            _register(cur, U_DEP, "deploy/Organ/1.0.0")
            _register(cur, U_A, "Prostate revision 1", prefix="PROS_PEL_CT_IO")
            _register(cur, U_B, "Prostate revision 2", prefix="PROS_PEL_CT_IO")     # same prefix: one dataset
        conn.commit()
        entries, _ = cat.load(conn)
        rows = dl.build(entries, dl.registered(conn), dl.person_links(conn))
    by = {r["name"]: r for r in rows}
    # Pancreas merged; CHAOS, the bundle, the prostate pair apart; the list's CMB-MML CT + PT rows one row (§102)
    assert len(rows) == len(entries) + 3 - 1
    cmb = by["CMB-MML"]
    assert len(cmb["entries"]) == 2 and cmb["tags"]["modality"] == ["CT", "PT"] and cmb["stand"] == "not_downloaded"
    pan = by["02 Pancreas-CT TCIA"]
    assert [d["dataset_uuid"] for d in pan["datasets"]] == [U_PAN] and pan["stand"] == "central"
    assert pan["registered"] == "yes" and pan["prefixes"] == ["PANC_ABD_CT_PUB"] and pan["n_central"] == 2
    assert pan["links"][0]["method"] == "intake_run" and pan["sources"] == ["list", "own"]
    chaos = by["CHAOS-MR"]
    assert chaos["registered"] == "no" and chaos["stand"] == "downloaded" and chaos["eff"]["commercial_use"] == "no"
    assert "proposal" in chaos["link_state"] and not chaos["datasets"]
    assert by["CHAOS"]["registered"] == "yes" and by["CHAOS"]["link_state"][0] == "registered_only"
    assert by["CHAOS"]["eff"]["commercial_use"] == "no" and by["CHAOS"]["eff"]["origin"] == "unknown"
    pros = by["Prostate revision 1"]
    assert len(pros["datasets"]) == 2 and pros["eff"]["origin"] == "internal" and pros["n_series"] == 2
    assert by["Never-Downloaded-Set"]["stand"] == "not_downloaded"
    sel, counts = dl.apply_filters(rows, {"commercial": ["yes"]})
    names = {r["name"] for r in sel}
    assert "02 Pancreas-CT TCIA" in names and "CHAOS-MR" not in names and "CHAOS" not in names
    assert counts["registered"] == {"yes": 1, "no": 2}                     # counts under the other filters
    counts = dl.apply_filters(rows, {})[1]
    assert counts["registered"]["yes"] == 4 and counts["stand"]["central"] == 1

    client = TestClient(create_app(settings))
    page = client.get("/datasets").text
    assert "Pancreas-CT (intake)" in page and "deploy/Organ/1.0.0" in page and "등록 · Central 전송" in page
    assert re.search(r"<b>4</b><span>.*관리 DB 등록 \(5개 데이터셋\)", page)
    r = client.get("/datasets?origin=deploy")                               # the old page's origin filter
    assert "deploy/Organ/1.0.0" in r.text and ">CHAOS-MR<" not in r.text and "배포 묶음" in r.text
    r = client.get("/datasets?registered=no&commercial=no")
    assert ">CHAOS-MR<" in r.text and ">CHAOS<" not in r.text
    assert "카탈로그: 02 Pancreas-CT TCIA" in client.get(f"/datasets/{U_PAN}").text
    chaos_id = chaos["entry_id"]
    r = client.get(f"/catalog/{chaos_id}")
    assert "이름 비슷 (후보)" in r.text and "같은 데이터셋" in r.text
    token = client.cookies.get("dataops_csrf")
    form = {"csrf": token, "dataset_uuid": U_CHAOS, "state": "confirmed", "by": ""}
    assert "err=" in client.post(f"/catalog/{chaos_id}/dataset", data=form, follow_redirects=False).headers["location"]
    r = client.post(f"/catalog/{chaos_id}/dataset", data={**form, "by": "lead"}, follow_redirects=True)
    assert "등록 데이터셋 연결을 저장했습니다" in r.text and "사람이 연결" in r.text
    page = client.get("/datasets?registered=yes").text                    # the write cleared the cache
    assert ">CHAOS-MR<" in page and "등록: CHAOS →" in page
    client.post(f"/catalog/{chaos_id}/dataset", data={**form, "state": "rejected", "by": "lead"})
    assert ">CHAOS-MR<" not in client.get("/datasets?registered=yes").text
    pan_id = pan["entry_id"]                                               # a person splits an intake-run link
    client.post(f"/catalog/{pan_id}/dataset", data={**form, "dataset_uuid": U_PAN, "state": "rejected", "by": "lead"})
    assert "02 Pancreas-CT TCIA" in client.get("/datasets?registered=no").text
    with dbmod.connection(test_db) as conn:
        assert dbmod.query(conn, "SELECT COUNT(*) AS n FROM catalog_dataset_links")[0]["n"] == 2
    h = {"x-forwarded-prefix": "/dataops-ui"}
    r = client.get("/catalog?registered=yes", headers=h, follow_redirects=False)
    assert r.headers["location"] == "/dataops-ui/datasets?registered=yes"
    page = client.get("/datasets", headers=h).text
    assert 'href="/dataops-ui/catalog/' in page and 'href="/datasets' not in page
