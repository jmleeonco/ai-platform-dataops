import json
import secrets
import socket
import sys
import threading
from pathlib import Path

import pytest
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import Verification

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataops.config import LocalScp, Settings, load_settings  # noqa: E402
from dataops.synthetic import make_mixed  # noqa: E402

DATASET = {"dataset_uuid": "11111111-2222-4333-8444-555555555555", "name": "Synthetic mixed",
           "license": "synthetic", "commercial_use": "N", "patient_id_prefix": "SYN_FULL_MULTI_IO"}


class LoopbackSCP:
    """Storage SCP on 127.0.0.1 that can refuse chosen SOP Instance UIDs."""

    def __init__(self, fail_sop_uids=()):
        self.received = {}
        self.fail = set(fail_sop_uids)
        self.lock = threading.Lock()
        ae = AE(ae_title="TESTSCP")
        ae.supported_contexts = StoragePresentationContexts
        ae.add_supported_context(Verification)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = ae.start_server(("127.0.0.1", self.port), block=False,
                                      evt_handlers=[(evt.EVT_C_STORE, self._on_store)])

    def _on_store(self, event):
        uid = str(event.request.AffectedSOPInstanceUID)
        if uid in self.fail:
            return 0xA700
        with self.lock:
            self.received[uid] = event.dataset
        return 0x0000

    def close(self):
        self.server.shutdown()

    def target(self) -> LocalScp:
        return LocalScp(host="127.0.0.1", port=self.port, called_ae="TESTSCP", calling_ae="DATAOPS_TEST")


@pytest.fixture
def scp():
    s = LoopbackSCP()
    yield s
    s.close()


@pytest.fixture
def mixed(tmp_path):
    return make_mixed(tmp_path / "input")


@pytest.fixture
def settings(tmp_path):
    return Settings(runtime_dir=tmp_path / "runtime", backup_root=tmp_path / "backups")


@pytest.fixture
def dataset_file(tmp_path):
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(DATASET), encoding="utf-8")
    return p


@pytest.fixture
def test_db():
    yield from _disposable_schema()


@pytest.fixture
def test_map_db():
    """A second disposable schema, standing in for the pseudonym map DB (its own schema in production)."""
    yield from _disposable_schema()


def _disposable_schema():
    """A disposable schema dataops_test_<random> on the configured dev DB; skipped when none is configured."""
    import pymysql
    base = load_settings()
    if not base.db_configured:
        pytest.skip("management DB not configured (deploy/db.sh init)")
    name = f"dataops_test_{secrets.token_hex(4)}"
    try:
        admin = pymysql.connect(host=base.db["host"], port=base.db["port"], user=base.db["user"],
                                password=base.db["password"], connect_timeout=3)
    except pymysql.MySQLError as e:
        pytest.skip(f"management DB not reachable: {e}")
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    db = dict(base.db, name=name)
    try:
        yield db
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        admin.close()
