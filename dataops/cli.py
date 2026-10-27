"""Command line: python -m dataops <command> ...

  plan DIR --dataset dataset.json [--group-by dir|file] [--modality CT|MR]   inventory + plan (new run)
  validate RUN                       re-validate plan.json after an edit (bumps the revision when it changed)
  prepare RUN                        freeze artifacts (copy DICOM / convert NIfTI, hash, manifest)
  verify RUN                         re-hash artifacts
  send-local RUN [--dry-run]         C-STORE the frozen artifacts to the configured LOCAL SCP
  register RUN                       write the run's records to the management DB
  ingest DIR --dataset ... [--send-local] [--register]   plan + prepare (+ send + register) in one go
  runs | show RUN                    list runs / print a run summary
  db-migrate | db-status             apply / show schema migrations
  serve                              local HTML viewer (default http://127.0.0.1:8096)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db as dbmod
from .config import load_settings
from .inventory import inventory
from .local_send import send_local
from .plan import build_plan, validate_plan
from .prepare import prepare, verify_artifacts
from .runs import Run, RunError, list_runs, new_run_id


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_plan(args, settings) -> Run:
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8-sig"))
    root = Path(args.dir).resolve()
    inv = inventory(root)
    run_id = new_run_id()
    options = {"nifti_group_by": args.group_by, "modality_override": args.modality}
    plan = build_plan(inv, dataset, run_id, options)
    run = Run.create(settings.runs_dir, plan["dataset"]["dataset_uuid"], str(root), run_id)
    run.save("inventory.json", inv)
    _, errors = validate_plan(plan, inv)
    meta = run.store_plan(plan, errors, "planned")
    active = [s for s in plan["series"] if not s.get("excluded")]
    print(f"run {run.id}: {meta['status']} (revision {meta['revision']})")
    print(f"  files {inv['counts']}, skipped dirs {inv['skipped_dirs']}")
    print(f"  subjects {len(plan['subjects'])}, studies {len(plan['studies'])}, series {len(active)}, "
          f"skipped files {len(plan['skipped'])}")
    for e in errors:
        print(f"  ! {e}")
    print(f"  plan: {run.path('plan.json')}")
    return run


def cmd_validate(args, settings):
    run = Run(settings.runs_dir, args.run)
    with run.lock():
        plan, errors = validate_plan(run.load("plan.json"), run.load("inventory.json"))
        meta = run.store_plan(plan, errors, "revalidated")
    print(f"run {run.id}: {meta['status']} (revision {meta['revision']})")
    for e in errors:
        print(f"  ! {e}")
    return 1 if errors else 0


def cmd_register(args, settings):
    if not settings.db_configured:
        raise RunError("DB 설정 없음 (~/.config/ai-platform-dataops/env 또는 DATAOPS_DB_* 환경변수)")
    run = Run(settings.runs_dir, args.run)
    with run.lock(), dbmod.connection(settings.db) as conn:
        status = dbmod.migration_status(conn)
        if status["pending"] or status["changed"]:
            raise RunError(f"DB schema가 최신이 아닙니다 ({status}) — `python -m dataops db-migrate` 먼저")
        stats = dbmod.register_run(conn, run)
    _print({"run": run.id, "registered": stats})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m dataops", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "ingest"):
        sp = sub.add_parser(name)
        sp.add_argument("dir")
        sp.add_argument("--dataset", required=True, help="dataset metadata JSON (see examples/dataset.json)")
        sp.add_argument("--group-by", choices=["dir", "file"], default="dir", help="NIfTI subject grouping")
        sp.add_argument("--modality", choices=["CT", "MR"], help="force NIfTI modality (recorded as plan option)")
        if name == "ingest":
            sp.add_argument("--send-local", action="store_true")
            sp.add_argument("--register", action="store_true")
    for name in ("validate", "prepare", "verify", "register", "show"):
        sub.add_parser(name).add_argument("run")
    sl = sub.add_parser("send-local")
    sl.add_argument("run")
    sl.add_argument("--dry-run", action="store_true")
    sub.add_parser("runs")
    sub.add_parser("db-migrate")
    sub.add_parser("db-status")
    sv = sub.add_parser("serve")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    args = p.parse_args(argv)
    settings = load_settings()
    try:
        if args.cmd in ("plan", "ingest"):
            run = cmd_plan(args, settings)
            if args.cmd == "ingest":
                if run.meta["status"] != "PLANNED":
                    print("plan에 해결할 항목이 있어 중단합니다 (plan.json 수정 후 validate → prepare).")
                    return 1
                m = prepare(run)
                print(f"prepared {len(m['instances'])} instances, manifest {m['manifest_sha256'][:16]}…")
                if args.send_local:
                    _print(send_local(run, settings.local_scp))
                if args.register:
                    cmd_register(argparse.Namespace(run=run.id), settings)
        elif args.cmd == "validate":
            return cmd_validate(args, settings)
        elif args.cmd == "prepare":
            m = prepare(Run(settings.runs_dir, args.run))
            print(f"prepared {len(m['instances'])} instances, manifest {m['manifest_sha256']}")
        elif args.cmd == "verify":
            problems = verify_artifacts(Run(settings.runs_dir, args.run))
            print("artifacts intact" if not problems else "\n".join(problems))
            return 1 if problems else 0
        elif args.cmd == "send-local":
            _print(send_local(Run(settings.runs_dir, args.run), settings.local_scp, dry_run=args.dry_run))
        elif args.cmd == "register":
            cmd_register(args, settings)
        elif args.cmd == "runs":
            for m in list_runs(settings.runs_dir):
                print(f"{m['run_id']}  {m['status'] or '-':14} rev {m['revision']}  {m.get('dataset_name') or ''}")
        elif args.cmd == "show":
            run = Run(settings.runs_dir, args.run)
            meta = run.meta
            meta.pop("events")
            _print({"run": meta, "plan_errors": run.load("plan_errors.json", []),
                    "registration": (run.load("registration.json") or {}).get("last")})
        elif args.cmd in ("db-migrate", "db-status"):
            if not settings.db_configured:
                raise RunError("DB 설정 없음")
            with dbmod.connection(settings.db) as conn:
                if args.cmd == "db-migrate":
                    print("applied:", dbmod.migrate(conn) or "nothing (up to date)")
                _print(dbmod.migration_status(conn) | {"counts": dbmod.table_counts(conn)})
        elif args.cmd == "serve":
            import uvicorn
            from .web.app import create_app
            uvicorn.run(create_app(settings), host=args.host or settings.bind_host, port=args.port or settings.port,
                        log_level="info")
    except (RunError, dbmod.DbError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    return 0
