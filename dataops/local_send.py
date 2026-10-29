"""Send a run's frozen artifacts to the LOCAL SCP, with a per-instance journal.

Reuses the inherited sender's local-host guard (this machine or a listed review workstation; never
Central) and C-STORE loop. Artifacts are re-hashed first; nothing is converted here.
scope "pilot" sends manifest-pilot.json (trial before approval), scope "full" sends manifest.json.
"""
from __future__ import annotations

import os
from collections import Counter

from .config import LocalScp
from .legacy import sender
from .prepare import manifest_name, verify_artifacts
from .runs import Run, RunError
from .util import now_iso, safe_name

STATUS_MAP = {"ok": "accepted", "warning": "warning", "failed": "failed"}
DONE = {"accepted", "warning"}
SEND_STATES = {
    "pilot": ({"PILOT_READY", "PILOT_SENT", "PILOT_PARTIAL", "PILOT_FAILED"}, "PILOT"),
    "full": ({"PREPARED", "LOCAL_SENT", "LOCAL_PARTIAL", "LOCAL_FAILED", "CENTRAL_SENT", "CENTRAL_PARTIAL",
              "CENTRAL_FAILED"}, "LOCAL"),
}


def destination(scp: LocalScp) -> "sender.Destination":
    cfg = {"destination": {"host": scp.host, "port": scp.port, "called_ae": scp.called_ae,
                           "calling_ae": scp.calling_ae}}
    try:
        return sender.destination_from(cfg, sender.DEFAULT_CONFIG, scp.review_hosts)
    except sender.ConfigError as e:
        raise RunError(str(e)) from None


def journal_name(target_key: str, scope: str = "full") -> str:
    base = safe_name(target_key.replace(':', '_').replace('@', '_'), 96)
    return f"transfers/{'pilot_' if scope == 'pilot' else ''}{base}.json"


def send_with_journal(run: Run, scope: str, target_key: str, target: dict, dest, dry_run: bool = False,
                      subjects: list[str] | None = None) -> dict:
    """C-STORE every not-yet-accepted instance of the scope's manifest to `dest`; journal each result.
    Caller holds the run lock and has checked the state. `subjects` limits the send (and the counts) to those
    subjects' series, e.g. the one sample chosen for the trial. Returns counts over what was asked for."""
    manifest = run.load(manifest_name(scope))
    if manifest is None:
        raise RunError(f"{scope} manifest 없음 (준비 전)")
    problems = verify_artifacts(run, manifest)
    if problems:
        raise RunError("artifact 검증 실패 — 전송하지 않습니다:\n- " + "\n- ".join(problems[:20]))
    if subjects:
        uids = {s["series_uid"] for s in manifest["series"] if s["subject_key"] in subjects}
        if not uids:
            raise RunError(f"{', '.join(subjects)}: 추출된 샘플이 아닙니다")
        manifest = {**manifest, "instances": [i for i in manifest["instances"] if i["series_uid"] in uids]}
    jname = journal_name(target_key, scope)
    journal = run.load(jname)
    if journal and journal["manifest_sha256"] != manifest["manifest_sha256"]:
        if scope != "pilot":
            raise RunError("전송 기록이 다른 manifest에 대한 것입니다")
        old = run.path(jname)                      # a re-frozen pilot starts a fresh journal
        archive = run.path("transfers/archive")
        archive.mkdir(parents=True, exist_ok=True)
        os.replace(old, archive / f"{old.stem}-{journal['manifest_sha256'][:12]}.json")
        journal = None
    journal = journal or {"target_key": target_key, "scope": scope, "manifest_sha256": manifest["manifest_sha256"],
                          "target": target, "instances": {}, "sessions": []}
    todo = [i for i in manifest["instances"] if journal["instances"].get(i["sop_uid"], {}).get("status") not in DONE]
    if dry_run:
        return {"target": target_key, "scope": scope, "to_send": len(todo),
                "already_done": len(manifest["instances"]) - len(todo)}
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
    outcome = "SENT" if done == total else ("FAILED" if done == 0 else "PARTIAL")
    return {"target": target_key, "scope": scope, "outcome": outcome, "counts": dict(counts),
            "sent_now": len(items), "total": total, "manifest_sha256": manifest["manifest_sha256"]}


def send_local(run: Run, scp: LocalScp, dry_run: bool = False, scope: str = "full",
               subjects: list[str] | None = None) -> dict:
    dest = destination(scp)                     # refuses non-local hosts before anything else
    allowed, prefix = SEND_STATES[scope]
    with run.lock():
        status = run.meta["status"]
        if status not in allowed:
            raise RunError(f"{'파일럿' if scope == 'pilot' else '로컬'} 전송은 {sorted(allowed)} 상태에서만 "
                           f"가능합니다 (현재 {status})")
        target = {"host": scp.host, "port": scp.port, "called_ae": scp.called_ae, "calling_ae": scp.calling_ae,
                  "kind": "local"}
        r = send_with_journal(run, scope, scp.key, target, dest, dry_run, subjects)
        if dry_run:
            return r
        new = f"{prefix}_{r['outcome']}"
        if scope == "full" and status.startswith("CENTRAL_"):
            new = status                        # a later local copy does not hide the publication state
        run.set_status(new, f"{scope}{' ' + ', '.join(subjects) if subjects else ''} → {scp.key}: {r['counts']}")
        return {**r, "status": new}


def journals(run: Run) -> list[dict]:
    d = run.path("transfers")
    if not d.is_dir():
        return []
    return [run.load(f"transfers/{p.name}") for p in sorted(d.glob("*.json"))]
