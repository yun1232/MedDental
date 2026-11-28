"""Strict allowlists for visible inputs and separate etiology/treatment targets."""
import hashlib
import json
import random
import re
from pathlib import Path

INPUT_FIELDS = {
    'history': ('既往史',),
    'clinical': ('既往史', '分布', '质地', '色调', '均一性'),
}
LABEL_FIELDS = ('病因', '治疗方案')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def case_number(value):
    value = str(value).strip()
    if not re.fullmatch(r'[0-9]+(?:\.0+)?', value):
        raise ValueError(f'Invalid case number: {value!r}')
    return str(int(value.split('.')[0]))


def field_name(value):
    return re.split(r'[（(]', str(value).strip(), maxsplit=1)[0].strip()


def read_cases(workbook, image_dir, sheet=None):
    from openpyxl import load_workbook
    from PIL import Image
    book = load_workbook(workbook, read_only=True, data_only=True)
    try:
        page = book[sheet] if sheet else book.worksheets[0]
        rows = iter(page.values)
        raw = list(next(rows))
        while raw and raw[-1] in (None, ''):
            raw.pop()
        headers = [field_name(h) if h is not None else '' for h in raw]
        required = {'编号', *INPUT_FIELDS['clinical'], *LABEL_FIELDS}
        if not headers or headers[0] != '编号' or not required.issubset(headers) or any(not h for h in headers) or len(set(headers)) != len(headers):
            raise ValueError('Workbook needs unique 编号、既往史、分布、质地、色调、均一性、病因、治疗方案 columns')
        images = {}
        for path in sorted(Path(image_dir).resolve().iterdir()):
            match = re.fullmatch(r'([0-9]+)(?:-\d+)?', path.stem)
            if path.is_file() and match and path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
                images.setdefault(case_number(match[1]), []).append(path)
        cases, excluded, seen = [], [], set()
        for row_number, row in enumerate(rows, 2):
            if row[0] is None:
                if any(x not in (None, '') for x in row):
                    raise ValueError(f'Row {row_number} has values without an ID')
                continue
            cid = case_number(row[0])
            if cid in seen:
                raise ValueError('Duplicate case number: ' + cid)
            seen.add(cid)
            fields = {k: str(v).strip() if v is not None else '' for k, v in zip(headers, row)}
            if not any(v for k, v in fields.items() if k != '编号'):
                excluded.append({'id': cid, 'reason': 'number_only'})
                continue
            missing = [k for k in LABEL_FIELDS if fields[k] in ('', '/', 'unknown', '不明')]
            if missing:
                excluded.append({'id': cid, 'reason': 'missing_labels', 'fields': missing})
                continue
            paths = images.get(cid, [])
            if not paths or len(paths) > 3:
                raise ValueError(f'Case {cid} needs one to three numbered images')
            entries = []
            for path in paths:
                with Image.open(path) as image:
                    image.verify()
                entries.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            cases.append({'id': cid, 'row': row_number,
                          'inputs': {k: fields[k] or '未记录' for k in INPUT_FIELDS['clinical']},
                          'labels': {k: fields[k] for k in LABEL_FIELDS}, 'images': entries})
        return cases, excluded, headers
    finally:
        book.close()


def public_case(case, input_mode):
    if input_mode not in INPUT_FIELDS:
        raise ValueError('Unknown input mode')
    return {'id': case['id'], 'text': {k: case['inputs'][k] for k in INPUT_FIELDS[input_mode]},
            'images': case['images']}


def split_cases(cases, seed=42, test_ratio=0.2):
    if not 0 < test_ratio < 1 or len(cases) < 2:
        raise ValueError("Use at least two labeled cases and 0 < test_ratio < 1")
    # Keep byte-identical images in one split, including transitive overlaps.
    groups, image_owner = {}, {}
    for case in sorted(cases, key=lambda c: int(c["id"])):
        related = {image_owner[i["sha256"]] for i in case["images"] if i["sha256"] in image_owner}
        group = min(related) if related else case["id"]
        merged = [case]
        for key in related:
            merged.extend(groups.pop(key))
        groups[group] = merged
        for member in merged:
            for image in member["images"]:
                image_owner[image["sha256"]] = group
    shuffled = list(groups.values())
    if len(shuffled) < 2:
        raise ValueError("All cases share images; cannot create a leakage-free test split")
    random.Random(seed).shuffle(shuffled)
    target = max(1, round(len(cases) * test_ratio))
    test, train = [], []
    for group in shuffled[:-1]:
        (test if len(test) < target else train).extend(c["id"] for c in group)
    train.extend(c["id"] for c in shuffled[-1])
    return {"train": train, "test": test}


def treatment_metrics(prediction, reference):
    """Transparent lexical proxy, not a clinical correctness judgment."""
    def norm(s):
        return re.sub(r"[\s，,。；;：:、（）()\-]", "", s).lower()
    def parts(s):
        return {norm(p) for p in re.split(r"[+＋、;；\n]", s) if norm(p)}
    predicted, target = parts(prediction), parts(reference)
    intersection = len(predicted & target)
    precision = intersection / len(predicted) if predicted else 0.0
    recall = intersection / len(target) if target else 0.0
    return {"normalized_exact_match": bool(predicted) and predicted == target,
            "component_precision": precision, "component_recall": recall,
            "component_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}
