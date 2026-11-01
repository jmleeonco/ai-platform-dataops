import json

from dataops.progress import job_progress


def test_progress_reads_conversion_then_sending(tmp_path):
    run, jobs = tmp_path / "run", tmp_path / "jobs"
    (run / "transfers").mkdir(parents=True)
    jobs.mkdir()
    job = {"id": "j1", "kind": "publish", "status": "running", "started_at": "2000-01-01T00:00:00+09:00",
           "params": {"target": "central:TESTSCP@192.0.2.1:4002"}}
    (jobs / "j1.log").write_text("00:00:01 [3/10] nifti:a.nii.gz\n00:00:02 [4/10] nifti:b.nii.gz\n")
    assert job_progress(run, job, jobs) == {"phase": "convert", "pct": 40.0, "text": "변환 4/10 시리즈"}

    man = {"manifest_sha256": "m", "instances": [{"sop_uid": f"{i}", "size": 1000} for i in range(4)]}
    (run / "manifest.json").write_text(json.dumps(man))
    (run / "transfers" / "central_TESTSCP_192.0.2.1_4002.json").write_text(json.dumps(
        {"manifest_sha256": "m", "instances": {"0": {"status": "accepted"}, "1": {"status": "sending"}}}))
    p = job_progress(run, job, jobs)
    assert p["phase"] == "send" and p["pct"] == 25.0 and "수신 기록 1개" in p["text"]
    assert job_progress(run, {**job, "status": "done"}, jobs) is None
