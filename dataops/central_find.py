"""Read-only C-FIND against Central (Study Root, SERIES level): what Central holds for a series UID.

Used to complete series that a list names by UID only (e.g. the deploy bundles' train_valid_dict.json) with the
study and patient Central has for them. Queries only; nothing is sent.
"""
from __future__ import annotations

from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind

from .config import CentralScp

RETURN_KEYS = ("StudyInstanceUID", "PatientID", "PatientName", "PatientSex", "PatientAge", "StudyDate",
               "StudyDescription", "Modality", "SeriesDescription", "SeriesNumber", "SeriesDate", "BodyPartExamined",
               "NumberOfSeriesRelatedInstances")


class CentralFindError(Exception):
    pass


def _value(v):
    if v is None or v == "":
        return None
    return str(v) if not isinstance(v, (int, float)) else v


class SeriesFinder:
    """One association; find(series_uid) -> list of matches (usually one), each a dict of RETURN_KEYS."""

    def __init__(self, central: CentralScp, timeout: float = 30):
        ae = AE(ae_title=central.calling_ae)
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = timeout
        self.assoc = ae.associate(central.host, int(central.port), ae_title=central.called_ae)
        if not self.assoc.is_established:
            raise CentralFindError(f"Central C-FIND 연결 실패 ({central.key})")

    def find(self, series_uid: str) -> list[dict]:
        q = Dataset()
        q.QueryRetrieveLevel = "SERIES"
        q.SeriesInstanceUID = series_uid
        for k in RETURN_KEYS:
            setattr(q, k, "")
        out = []
        for status, ident in self.assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind):
            if status is None:
                raise CentralFindError("Central C-FIND 응답 없음")
            if status.Status in (0xFF00, 0xFF01) and ident is not None:
                out.append({k: _value(ident.get(k)) for k in RETURN_KEYS})
            elif status.Status not in (0x0000, 0xFF00, 0xFF01):
                raise CentralFindError(f"Central C-FIND 상태 0x{status.Status:04X}")
        return out

    def close(self):
        if self.assoc.is_established:
            self.assoc.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
