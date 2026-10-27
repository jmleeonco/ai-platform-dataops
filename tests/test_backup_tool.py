import datetime as dt
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup_tool", ROOT / "ops/backup/backup_tool.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


def days(n, end="2026-10-28"):
    e = dt.date.fromisoformat(end)
    return [(e - dt.timedelta(days=i)).isoformat() for i in range(n)]


def test_keep_14_daily_plus_first_of_month_for_12_months():
    all_days = days(500)
    keep = bt.keep_set(all_days, "2026-10-28")
    assert set(all_days[:14]) <= keep
    firsts = sorted(d for d in keep if d not in all_days[:14])
    # first folder of each of the 12 most recent months (Oct's first is inside the daily window? no: 10-01)
    assert len({d[:7] for d in keep}) == 12 and all(d.endswith("-01") for d in firsts)
    assert "2025-06-01" not in keep


def test_today_never_pruned_and_only_date_dirs(tmp_path):
    base = tmp_path / "ai-platform-dataops"
    base.mkdir()
    for d in days(40):
        (base / d).mkdir()
    (base / "backup.log").write_text("x")
    (base / "not-a-date").mkdir()
    removed = bt.prune(base, "2026-10-28", dry_run=False)
    names = {p.name for p in base.iterdir()}
    assert "2026-10-28" in names and "not-a-date" in names and "backup.log" in names
    # folders 09-19..10-28: keep 14 daily (10-15..10-28) + first of October (10-01) + first of September (09-19)
    assert len(removed) == 40 - 16
    assert {"2026-10-01", "2026-09-19"} <= names and "2026-09-20" in removed


def test_manifest_and_verify(tmp_path):
    work = tmp_path / "w"
    (work / "db").mkdir(parents=True)
    (work / "db" / "x.sql.gz").write_bytes(b"abc")
    (work / "db" / "row_counts.tsv").write_text("series\t4\ninstances\t28\n")
    m = bt.manifest(work, 0.0, "deadbeef", True, None, {"db_name": "public_dataops"})
    assert m["db_counts"] == {"series": 4, "instances": 28} and m["ok"]
    assert bt.verify_files(work) == []
    (work / "db" / "x.sql.gz").write_bytes(b"abd")
    assert bt.verify_files(work) == ["db/x.sql.gz"]
    assert json.loads((work / "manifest.json").read_text())["commit"] == "deadbeef"
