"""Offline metadata preview only. No network, SQL execution, approval or publishing."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from pathlib import Path
from uuid import UUID, uuid5


def uid(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError('Missing or too long DICOM UID')
    if not re.fullmatch(r'(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))+', value):
        raise ValueError('Invalid DICOM UID')
    return value


def project(summary: dict, dataset: dict) -> dict:
    """Project supported metadata to the proposed schema, never fabricate instances."""
    namespace = UUID(dataset['dataset_uuid'])
    name = dataset.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise ValueError('dataset.name must be 1..256 characters')
    if dataset.get('commercial_use', 'U') not in ('Y', 'N', 'U'):
        raise ValueError('commercial_use must be Y, N or U')
    ds = {key: dataset.get(key) for key in ('name', 'source_url', 'license', 'description')}
    ds.update(dataset_uuid=str(namespace), commercial_use=dataset.get('commercial_use', 'U'))
    rows = summary.get('series')
    if not isinstance(rows, list) or not rows:
        raise ValueError('No series to preview')
    subjects, studies, series, members = {}, {}, {}, []
    for row in rows:
        pid = row.get('patient_id')
        if not isinstance(pid, str) or not pid.strip() or len(pid) > 64:
            raise ValueError('patient_id must be 1..64 characters for LAB compatibility')
        suid, seruid = uid(row.get('study_uid')), uid(row.get('series_uid'))
        modality = row.get('modality')
        if not isinstance(modality, str) or not modality.strip() or len(modality) > 16:
            raise ValueError('modality is required')
        subject_id = str(uuid5(namespace, pid))
        # This adapter uses patient_id as source key. Full ingestion must record real source lineage.
        subjects[pid] = dict(subject_uuid=subject_id, dataset_uuid=str(namespace),
                             source_subject_key=pid, patient_id=pid)
        study = dict(study_uid=suid, subject_uuid=subject_id)
        if suid in studies and studies[suid] != study:
            raise ValueError('Same Study UID maps to different patients')
        studies[suid] = study
        if seruid in series:
            raise ValueError('Duplicate Series UID in preview')
        series[seruid] = dict(series_uid=seruid, study_uid=suid, modality=modality,
                              description=row.get('series_description', ''), metadata_json=None)
        members.append(dict(dataset_uuid=str(namespace), series_uid=seruid))
    canonical = json.dumps({'summary': summary, 'dataset': dataset}, sort_keys=True,
                           ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {
        'schema_version': 1, 'preview_only': True,
        'metadata_sha256': hashlib.sha256(canonical).hexdigest(),
        'limitations': ['No instance manifest or file hashes: NOT publishable.',
                        'Counts/status come from sender summary; no independent receiver verification.',
                        'No image viewer, approval or DB writes in this offline tool.'],
        'candidate_records': {'datasets': [ds], 'subjects': list(subjects.values()),
                              'studies': list(studies.values()), 'series': list(series.values()),
                              'dataset_series': members},
        'local_summary': summary,
    }


def render(document: dict) -> str:
    esc = html.escape
    rows = document['candidate_records']
    parts = ['<!doctype html><html lang="ko"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
             '<title>DataOps 등록 미리보기</title>',
             '<style>body{font:16px system-ui;max-width:1150px;margin:32px auto;padding:0 20px;color:#182b40;background:#f5f7fa} '
             'section{background:white;padding:20px;margin:20px 0;border:1px solid #d4dce5;border-radius:8px} '
             'pre{white-space:pre-wrap;overflow-wrap:anywhere} .notice{border-left:5px solid #d89520;padding:12px;background:#fff3d6}</style>',
             '<h1>공공데이터 DB 등록 미리보기</h1>',
             '<p class="notice">정적 메타데이터 미리보기입니다. 영상 검토·승인·Central 전송·DB 저장은 실행되지 않습니다.</p>',
             '<h2>' + esc(rows['datasets'][0]['name']) + '</h2>',
             '<p>대상자 ' + str(len(rows['subjects'])) + ' · 검사 ' + str(len(rows['studies'])) +
             ' · 시리즈 ' + str(len(rows['series'])) + '</p>']
    for title, value in [('검토 범위와 제한', document['limitations']),
                         ('로컬 전송 결과 / 누락·오류', document['local_summary']), *rows.items()]:
        parts.append('<section><h2>' + esc(title) + '</h2><pre>' +
                     esc(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre></section>')
    parts.append('<p>Metadata fingerprint: ' + esc(document['metadata_sha256']) + '</p></html>')
    return ''.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        doc = project(json.loads(args.summary.read_text(encoding='utf-8-sig')),
                      json.loads(args.dataset.read_text(encoding='utf-8-sig')))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    args.out.mkdir(parents=True, exist_ok=True)
    # Refuse overwriting either input, even if the user selected a colliding output path.
    outputs = [args.out / 'candidate-records.json', args.out / 'review.html']
    if {p.resolve() for p in outputs} & {args.summary.resolve(), args.dataset.resolve()}:
        parser.error('Output would overwrite input')
    outputs[0].write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    outputs[1].write_text(render(doc), encoding='utf-8')
    print(outputs[1].resolve())


if __name__ == '__main__':
    main()
