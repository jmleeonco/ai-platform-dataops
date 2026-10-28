"""Send a prepared run's frozen artifacts to the LOCAL SCP, with a per-instance journal.

Reuses the inherited sender's local-host guard (this machine or a listed review workstation; never
Central) and C-STORE loop.
Artifacts are re-hashed first; nothing is converted here.
"""
from __future__ import annotations

from collections import Counter

from .config import LocalScp
from .legacy import sender
from .prepare import verify_artifacts
from .runs import Run, RunError
from .util import now_iso, safe_name

STATUS_MAP = {"ok": "accepted", "warning": "warning", "failed": "failed"}
DONE = {"accepted", "warning"}


def destination(scp: LocalScp) -> "sender.Destination":
    cfg = {"destination": {"host": scp.host, "port": scp.port, "called_ae": scp.called_ae,
                           "calling_ae": scp.calling_ae}}
    try:
        return sender.destination_from(cfg, sender.DEFAULT_CONFIG, scp.review_hosts)
    except sender.ConfigError as e:
        raise RunError(str(e)) from None


def journal_name(scp: LocalScp) -> str:
    return f"transfers/{safe_name(scp.key.replace(':', '_').replace('@', '_'), 96)}.json"


def send_local(run: Run, scp: LocalScp, dry_run: bool = False) -> dict:
    dest = destination(scp)                     # refuses non-local hosts before anything else
    with run.lock():
        meta = run.meta
        if meta["status"] not in ("PREPARED", "LOCAL_SENT", "LOCAL_PARTIAL", "LOCAL_FAILED"):
            raise RunError(f"로컬 전송은 준비된 run에서만 가능합니다 (현재 {meta['status']})")
        manifest = run.load("manifest.json")
        problems = verify_artifacts(run, manifest)
        if problems:
            raise RunError("artifact 검증 실패 — 전송하지 않습니다:\n- " + "\n- ".join(problems[:20]))
        jname = journal_name(scp)
        journal = run.load(jname) or {"target_key": scp.key, "manifest_sha256": manifest["manifest_sha256"],
                                      "target": {"host": scp.host, "port": scp.port, "called_ae": scp.called_ae,
                                                 "calling_ae": scp.calling_ae, "kind": "local"},
                                      "instances": {}, "sessions": []}
        if journal["manifest_sha256"] != manifest["manifest_sha256"]:
            raise RunError("전송 기록이 다른 manifest에 대한 것입니다")
        todo = [i for i in manifest["instances"] if journal["instances"].get(i["sop_uid"], {}).get("status") not in DONE]
        if dry_run:
            return {"target": scp.key, "to_send": len(todo), "already_done": len(manifest["instances"]) - len(todo)}
        for inst in todo:                       # durable "sending" marker: a crash leaves an explicit unknown
            rec = journal["instances"].setdefault(inst["sop_uid"], {"attempts": 0})
            rec.update(status="sending", updated_at=now_iso())
        run.save(jname, journal)
        items = [sender.SendItem(run.dir / i["path"], i["sop_class_uid"], i["sop_uid"],
                                 i["transfer_syntax"] or str(sender.ImplicitVRLittleEndian), i["series_uid"])
                 for i in todo]
        started = now_iso()
        if items:
            sender.send_items(dest, items)
        for it in items:
            rec = journal["instances"][it.sop_uid]
            rec.update(status=STATUS_MAP.get(it.status, "failed"), detail=it.detail, attempts=rec["attempts"] + 1,
                       updated_at=now_iso())
        counts = Counter(journal["instances"][i["sop_uid"]]["status"] for i in manifest["instances"])
        journal["sessions"].append({"started_at": started, "finished_at": now_iso(), "sent": len(items),
                                    "result": dict(Counter(STATUS_MAP.get(i.status, "failed") for i in items))})
        journal["updated_at"] = now_iso()
        run.save(jname, journal)
        total = len(manifest["instances"])
        done = counts["accepted"] + counts["warning"]
        status = "LOCAL_SENT" if done == total else ("LOCAL_FAILED" if done == 0 else "LOCAL_PARTIAL")
        run.set_status(status, f"local {scp.key}: {dict(counts)}")
        return {"target": scp.key, "status": status, "counts": dict(counts), "sent_now": len(items), "total": total}


def journals(run: Run) -> list[dict]:
    d = run.path("transfers")
    if not d.is_dir():
        return []
    return [run.load(f"transfers/{p.name}") for p in sorted(d.glob("*.json"))]
