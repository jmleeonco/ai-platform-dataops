import json
import shutil

import pydicom

from dataops import deploy_import as di
from dataops.synthetic import write_ct_series


def test_bundles_become_datasets_from_their_files_the_db_and_central(tmp_path, monkeypatch):
    root = tmp_path / "deploy"
    monkeypatch.setattr(di, "ROOT", root)
    b1, b2 = root / "Organ" / "1.0.0", root / "JsonOnly" / "0.0.1"
    write_ct_series(b1 / "dicom" / "P001" / "ct", "SYN_P001", seed=1)
    write_ct_series(b1 / "dicom" / "P002" / "ct", "SYN_P002", seed=2)
    (b1 / "dicom" / "notes.txt").write_text("not dicom")
    files = sorted((b1 / "dicom" / "P001" / "ct").iterdir())
    ct1 = str(pydicom.dcmread(files[0]).SeriesInstanceUID)
    (b1 / "train_valid_dict.json").write_text(json.dumps({
        "train": [{"patient_id": "SYN_P001", "dicom_series_instance_uid": ct1, "rtss_series_instance_uid": ["1.9.9"]}],
        "valid": [{"patient_id": "SYN_P002", "dicom_series_instance_uid": "00c6735b-ae77-4747-830c-f92ffb53feab",
                   "rtss_series_instance_uid": "1.9.8"}]}))
    (b1 / "deploy_info.json").write_text(json.dumps({"deploys": [{"memo": "first"}, {"memo": "second"}]}))
    b2.mkdir(parents=True)
    (b2 / "train_valid_dict.json").write_text(json.dumps({"train": [{"patientId": 7, "image": "nifti/a.nii.gz"}]}))

    b4 = root / "ListOnly"                                                       # only a list-shaped dataset.json
    b4.mkdir()
    (b4 / "dataset.json").write_text(json.dumps([{"patient_id": "L1", "image": "nifti/x.nii.gz"},
                                                  {"patient_id": "", "patient_name": "L2"}]))
    (root / di.HISTORY).write_text(json.dumps({"model_name": "JsonOnly", "version": "0.0.1", "memo": "old"}) + "\n"
                                   + json.dumps({"model_name": "JsonOnly", "version": "0.0.1", "memo": "from history"}))
    assert di.bundles() == [b2, b4, b1]
    assert di.bundle_info(b2) == {"memo": "from history", "deployed_at": None, "list": "train_valid_dict.json"}
    assert di.json_entries(b4) == [{"split": "전체", "patient_id": "L1", "patient_name": None, "image_uid": None,
                                    "rtss_uids": []},
                                   {"split": "전체", "patient_id": "L2", "patient_name": "L2", "image_uid": None,
                                    "rtss_uids": []}]                          # no patient_id: the name stands in
    scans = {str(b.relative_to(root)): di.scan_folder(b, tmp_path / "cache") for b in (b1, b2)}
    s1 = scans["Organ/1.0.0"]
    assert len(s1["series"]) == 2 and s1["stats"] == {"DICOM 아님/읽기 실패": 1}
    assert s1["series"][ct1]["files"] == len(files) and s1["series"][ct1]["dir"] == "dicom/P001/ct"
    assert di.scan_folder(b1, tmp_path / "cache") == s1                       # cached
    b3 = root / "Copy" / "1.0.0"
    shutil.copytree(b1 / "dicom", b3 / "dicom")
    s3 = di.scan_folder(b3, tmp_path / "cache", log=lambda m: None)
    assert s3["same_as"] == str(b1 / "dicom") and s3["series"] == s1["series"]   # a copy is not read again

    found = {"1.9.9": di.central_series({"StudyInstanceUID": "1.8", "PatientID": "SYN_P001", "Modality": "RTSTRUCT",
                                         "SeriesNumber": "2", "NumberOfSeriesRelatedInstances": "1"})}
    entries = {str(b.relative_to(root)): (di.bundle_info(b), di.json_entries(b)) for b in (b2, b1)}
    rows, report = di.build_rows(entries, scans, found, {"series": set(), "studies": set()})
    ds = {d["name"]: d for d in rows["datasets"]}
    assert set(ds) == {"deploy/Organ/1.0.0", "deploy/JsonOnly/0.0.1"} and ds["deploy/Organ/1.0.0"]["description"] == \
        "second · train_valid_dict.json: train 1, valid 1"
    series = {s["series_uid"]: s for s in rows["series"]}
    assert series[ct1]["source_kind"] == "deploy" and series[ct1]["instance_count"] == len(files)
    assert series["1.9.9"]["source_kind"] == "central-find" and series["1.9.9"]["modality"] == "RTSTRUCT"
    assert report["Organ/1.0.0"]["json의 UID가 폴더·DB·Central 어디에도 없음"] == 1          # 1.9.8
    assert report["JsonOnly/0.0.1"]["시리즈 없이 환자만 (json에 UID·폴더 없음)"] == 1
    org = di.dataset_uuid("Organ/1.0.0")
    assert {x["series_uid"] for x in rows["dataset_series"] if x["dataset_uuid"] == org} == {ct1, "1.9.9", *s1["series"]}
    assert {s["patient_id"] for s in rows["subjects"]} == {"SYN_P001", "SYN_P002", "7"}
    split = {x["series_uid"]: x["split"] for x in rows["dataset_series"] if x["dataset_uuid"] == org}
    p2 = next(u for u, s in s1["series"].items() if s["PatientID"] == "SYN_P002")
    assert (split[ct1], split["1.9.9"], split[p2]) == ("train", "train", "valid")   # by UID; P002's by patient
    assert report["Organ/1.0.0"]["split"] == {"train": 2, "valid": 1}

    rows2, report2 = di.build_rows(entries, scans, found, {"series": {ct1}, "studies": {series[ct1]["study_uid"]}})
    assert ct1 not in {s["series_uid"] for s in rows2["series"]}                # already registered: link only
    assert report2["Organ/1.0.0"]["이미 DB에 있는 시리즈 (연결만)"] == 1
