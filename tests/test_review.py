import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review', ROOT / 'tools/review.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def inputs():
    return (json.loads((ROOT / 'examples/synthetic-summary.json').read_text()),
            json.loads((ROOT / 'examples/dataset.json').read_text()))


def test_preview_is_stable_and_scoped():
    summary, dataset = inputs()
    a = review.project(summary, dataset)
    assert a == review.project(summary, dataset)
    assert a['preview_only']
    records = a['candidate_records']
    assert records['studies'][0]['subject_uuid'] == records['subjects'][0]['subject_uuid']
    dataset['dataset_uuid'] = '47939f9c-6ca5-4618-b6ed-d3e8fd7696cb'
    b = review.project(summary, dataset)
    assert records['subjects'][0]['subject_uuid'] != b['candidate_records']['subjects'][0]['subject_uuid']


def test_xss_and_metadata_revision():
    summary, dataset = inputs()
    old = review.project(summary, dataset)
    dataset['name'] = '<script>alert(1)</script>'
    new = review.project(summary, dataset)
    rendered = review.render(new)
    assert '<script>' not in rendered
    assert '&lt;script&gt;' in rendered
    assert old['metadata_sha256'] != new['metadata_sha256']


def test_conflicting_study_rejected():
    summary, dataset = inputs()
    row = copy.deepcopy(summary['series'][0])
    row.update(patient_id='OTHER', series_uid='2.25.103')
    summary['series'].append(row)
    with pytest.raises(ValueError, match='different patients'):
        review.project(summary, dataset)


@pytest.mark.parametrize('field,value', [('study_uid','bad'), ('series_uid','1.02.3'),
                                       ('patient_id','x'*65), ('modality','')])
def test_missing_or_invalid_identity(field, value):
    summary, dataset = inputs()
    summary['series'][0][field] = value
    with pytest.raises(ValueError):
        review.project(summary, dataset)


def test_duplicate_series_rejected():
    summary, dataset = inputs()
    summary['series'].append(copy.deepcopy(summary['series'][0]))
    with pytest.raises(ValueError, match='Duplicate'):
        review.project(summary, dataset)
