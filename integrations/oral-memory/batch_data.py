"""Workbook boundary: numbered cases, diagnosis facts, hidden treatment labels."""
import hashlib
import json
import random
import re
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def case_number(value):
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]+(?:\.0+)?", value):
        raise ValueError(f"Invalid case number: {value!r}")
    return str(int(float(value)))


def read_cases(workbook, image_dir, sheet=None):
    from openpyxl import load_workbook
    from PIL import Image
    book = load_workbook(workbook, read_only=True, data_only=True)
    try:
        page = book[sheet] if sheet else book.worksheets[0]
        rows = iter(page.values)
        raw = list(next(rows))
        while raw and raw[-1] in (None, ""):
            raw.pop()
        headers = [str(h).strip() if h is not None else "" for h in raw]
        if len(headers) < 3 or headers[0] != "编号" or headers[-1] != "治疗方案":
            raise ValueError("First effective column must be 编号; last must be 治疗方案")
        if any(not h for h in headers) or len(set(headers)) != len(headers):
            raise ValueError("Nonempty, unique effective column names are required")
        image_dir = Path(image_dir).resolve()
        images = {}
        for path in sorted(image_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png"):
                match = re.fullmatch(r"([0-9]+)(?:-\d+)?", path.stem)
                if match:
                    images.setdefault(case_number(match[1]), []).append(path)
        cases, excluded, seen = [], [], set()
        for row_number, row in enumerate(rows, 2):
            if row[0] is None:
                if any(x not in (None, "") for x in row):
                    raise ValueError(f"Row {row_number} contains data without a case number")
                continue
            cid = case_number(row[0])
            if cid in seen:
                raise ValueError(f"Duplicate case number: {cid}")
            seen.add(cid)
            values = [str(v).strip() if v is not None else "" for v in row[:len(headers)]]
            if not any(values[1:]):
                excluded.append({"id": cid, "reason": "number_only"})
                continue
            label = values[-1]
            if label in ("", "/", "unknown", "不明"):
                excluded.append({"id": cid, "reason": "missing_treatment"})
                continue
            paths = images.get(cid, [])
            if not paths:
                raise ValueError(f"Case {cid} has no image named {cid}.jpg or {cid}-N.jpg")
            if len(paths) > 3:
                raise ValueError(f"Case {cid} exceeds the existing three-image limit")
            facts, metadata = {}, {}
            for header, value in zip(headers[1:-1], values[1:-1]):
                # Source names in this workbook contain patient names AND treatment labels.
                # Preserve the column's existence without leaking either into model prompts.
                if header in ("文件名", "图片名", "图像名", "姓名", "患者姓名"):
                    metadata[header] = "[source metadata withheld; images bound by 编号]"
                else:
                    facts[header] = value or "未记录"
            image_entries = []
            for path in paths:
                with Image.open(path) as image:
                    image.verify()
                image_entries.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            cases.append({"id": cid, "row": row_number, "facts": facts, "metadata": metadata,
                          "images": image_entries, "reference_treatment": label})
        return cases, excluded, headers
    finally:
        book.close()


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


def public_case(case, mode, max_turns):
    """This is the ONLY per-case document admitted to the Harness subprocess."""
    return {"id": case["id"], "mode": mode, "max_turns": max_turns,
            "facts": case["facts"], "metadata": case["metadata"],
            "images": [i["path"] for i in case["images"]]}


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
