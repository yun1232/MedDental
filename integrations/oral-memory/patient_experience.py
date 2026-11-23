"""One patient-experience category over legacy oral/care storage identities.

The backing references stay unchanged so historical versions, scores and receipts
remain valid, including when both old tables use the same record_id.
"""
from schemas import MemoryError, validate, PATIENT_EXPERIENCE_WRITE


def normalize(record):
    origin = record.get('storage_reference', {}).get('category', record['category'])
    if origin not in ('experience', 'care'):
        raise MemoryError('invalid_category', 'Patient experience contains oral or care events')
    return {**record, 'category': 'patient_experience', 'memory_category': 'patient_experience',
            'event_kind': 'care' if origin == 'care' else 'oral',
            'storage_reference': {'category': origin, 'record_id': record['record_id'], 'version': record['version']}}


def put(store, arguments):
    validate(arguments, PATIENT_EXPERIENCE_WRITE)
    record = dict(arguments['record'])
    kind = record.pop('event_kind')
    if kind == 'care':
        if 'care_type' not in record or 'experience_type' in record:
            raise MemoryError('invalid_input', 'Care events require care_type and cannot carry experience_type')
        record.setdefault('outcome', '')
        origin = 'care'
    else:
        if 'care_type' in record or 'outcome' in record:
            raise MemoryError('invalid_input', 'Oral events cannot carry care-only fields')
        record.setdefault('experience_type', 'other_oral')
        origin = 'experience'
    result = store.put(origin, {'record': record, 'expected_version': arguments['expected_version']})
    return {**result, 'record': normalize(result['record'])}
