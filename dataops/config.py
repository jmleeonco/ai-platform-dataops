"""Settings: environment variables override ~/.config/ai-platform-dataops/env, which overrides defaults.

Secrets (DB password) are only read from the environment or that file, never from the repository.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(os.environ.get("DATAOPS_CONFIG_DIR", Path.home() / ".config" / "ai-platform-dataops"))
# Owner decision 2026-10-28: the local SCP is OncoStudio's OS_LJM on the Windows test PC, not on this server.
# Only addresses listed here (plus this machine) pass the sender's host guard.
REVIEW_WORKSTATION = "10.101.1.107"


def _read_env_file(path: Path) -> dict:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass
class LocalScp:
    host: str
    port: int
    called_ae: str
    calling_ae: str
    review_hosts: tuple = ()        # remote hosts the local-host guard accepts (exact IPs)

    @property
    def key(self) -> str:
        return f"local:{self.called_ae}@{self.host}:{self.port}"


@dataclass
class CentralScp:
    """Central SCP for approved publication. Unset (or enabled=False) means publication is refused."""
    host: str | None = None
    port: int | None = None
    called_ae: str | None = None
    calling_ae: str = "DATAOPS"
    enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port and self.called_ae)

    @property
    def key(self) -> str:
        return f"central:{self.called_ae}@{self.host}:{self.port}"


INTERNAL_ROOTS = ("/mnt2/data/Onco_dataset", "/mnt2-v3-test/data/Onco_dataset")


@dataclass
class Settings:
    runtime_dir: Path
    db: dict = field(default_factory=dict)
    # The pseudonymisation map (original <-> pseudonym): its own schema and account, apart from the management DB.
    deid_map_db: dict = field(default_factory=dict)
    legacy_db: dict = field(default_factory=dict)      # existing production DB, read for its shape only
    local_scp: LocalScp | None = None
    bind_host: str = "127.0.0.1"
    port: int = 8096
    backup_root: Path = Path("/mnt/ai-pipeline/backups")
    backup_system: str = "ai-platform-dataops"
    central: CentralScp = field(default_factory=CentralScp)
    input_roots: tuple = ("/mnt2/data/Public_dataset",)
    # Internal (hospital / in-house) data: analysed with pseudonymisation on unless the requester turns it off.
    internal_roots: tuple = INTERNAL_ROOTS
    agent_cmd: str = "claude"
    agent_model: str = "claude-opus-5-5"
    agent_budget_usd: float = 5.0
    agent_timeout_s: int = 1800
    agent_workdir: Path = Path.home() / ".cache" / "ai-platform-dataops" / "agent"

    @property
    def jobs_dir(self) -> Path:
        return self.runtime_dir / "jobs"

    @property
    def runs_dir(self) -> Path:
        return self.runtime_dir / "runs"

    @property
    def db_configured(self) -> bool:
        return all(self.db.get(k) for k in ("host", "user", "password", "name"))

    @property
    def deid_map_configured(self) -> bool:
        return all(self.deid_map_db.get(k) for k in ("host", "user", "password", "name"))


def load_settings(env: dict | None = None) -> Settings:
    merged = _read_env_file(CONFIG_DIR / "env")
    merged.update({k: v for k, v in (env if env is not None else os.environ).items() if k.startswith("DATAOPS_")})
    get = merged.get
    runtime = Path(get("DATAOPS_RUNTIME_DIR") or REPO_DIR / "runtime")
    if not runtime.is_absolute():
        runtime = REPO_DIR / runtime
    example = json.loads((REPO_DIR / "config" / "local-scp.example.json").read_text(encoding="utf-8"))["destination"]
    review = get("DATAOPS_LOCAL_SCP_REVIEW_HOSTS")
    scp = LocalScp(host=get("DATAOPS_LOCAL_SCP_HOST") or REVIEW_WORKSTATION,
                   port=int(get("DATAOPS_LOCAL_SCP_PORT") or example["port"]),
                   called_ae=get("DATAOPS_LOCAL_SCP_CALLED_AE") or example["called_ae"],
                   calling_ae=get("DATAOPS_LOCAL_SCP_CALLING_AE") or "DATAOPS",
                   review_hosts=tuple(h for h in (review if review is not None else REVIEW_WORKSTATION)
                                      .replace(",", " ").split()))
    return Settings(
        runtime_dir=runtime,
        db={"host": get("DATAOPS_DB_HOST"), "port": int(get("DATAOPS_DB_PORT") or 3306),
            "name": get("DATAOPS_DB_NAME"), "user": get("DATAOPS_DB_USER"), "password": get("DATAOPS_DB_PASSWORD")},
        deid_map_db={"host": get("DATAOPS_DEIDMAP_DB_HOST") or get("DATAOPS_DB_HOST"),
                     "port": int(get("DATAOPS_DEIDMAP_DB_PORT") or get("DATAOPS_DB_PORT") or 3306),
                     "name": get("DATAOPS_DEIDMAP_DB_NAME") or "dataops_deid_map",
                     "user": get("DATAOPS_DEIDMAP_DB_USER"), "password": get("DATAOPS_DEIDMAP_DB_PASSWORD")},
        legacy_db={"host": get("DATAOPS_LEGACY_DB_HOST"), "port": int(get("DATAOPS_LEGACY_DB_PORT") or 3306),
                   "user": get("DATAOPS_LEGACY_DB_USER"), "password": get("DATAOPS_LEGACY_DB_PASSWORD"),
                   "schemas": tuple(x for x in (get("DATAOPS_LEGACY_DB_SCHEMAS") or "scpdb,oncolabdb")
                                    .replace(",", " ").split())},
        local_scp=scp,
        bind_host=get("DATAOPS_BIND_HOST") or "127.0.0.1",
        port=int(get("DATAOPS_PORT") or 8096),
        backup_root=Path(get("DATAOPS_BACKUP_ROOT") or "/mnt/ai-pipeline/backups"),
        backup_system=get("DATAOPS_BACKUP_SYSTEM") or "ai-platform-dataops",
        central=CentralScp(host=get("DATAOPS_CENTRAL_HOST") or None,
                           port=int(get("DATAOPS_CENTRAL_PORT")) if get("DATAOPS_CENTRAL_PORT") else None,
                           called_ae=get("DATAOPS_CENTRAL_CALLED_AE") or None,
                           calling_ae=get("DATAOPS_CENTRAL_CALLING_AE") or "DATAOPS",
                           enabled=(get("DATAOPS_CENTRAL_ENABLED") or "").lower() in ("1", "true", "yes")),
        input_roots=tuple(r for r in (get("DATAOPS_INPUT_ROOTS") or "/mnt2/data/Public_dataset").split(":") if r),
        internal_roots=tuple(r for r in (get("DATAOPS_INTERNAL_ROOTS") or ":".join(INTERNAL_ROOTS)).split(":") if r),
        agent_cmd=get("DATAOPS_AGENT_CMD") or shutil.which("claude") or str(Path.home() / ".local/bin/claude"),
        agent_model=get("DATAOPS_AGENT_MODEL") or "claude-opus-5-5",
        agent_budget_usd=float(get("DATAOPS_AGENT_BUDGET_USD") or 5),
        agent_timeout_s=int(get("DATAOPS_AGENT_TIMEOUT_S") or 1800),
        agent_workdir=Path(get("DATAOPS_AGENT_WORKDIR") or Path.home() / ".cache" / "ai-platform-dataops" / "agent"),
    )
