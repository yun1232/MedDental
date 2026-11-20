"""Independent low-cost descriptor and text matching with strict priority ordering."""

import json
import re
import unicodedata
from extensions import select_ties

UNKNOWN = {"unknown", "未知", "待确认", "不详", "无法判断", "未提供", ""}
ALIASES = {"颊部黏膜": "颊黏膜", "白色斑片": "白色", "斑片状": "片状",
           "左": "left", "右": "right", "双侧": "bilateral", "中线": "midline"}


def normalize(value):
    """Normalize orthography and a small explicit descriptor synonym dictionary."""
    value = unicodedata.normalize("NFKC", value).strip().lower()
    return ALIASES.get(value, value)


def known(value):
    """Distinguish unknown values from observed descriptor values."""
    return normalize(value) not in UNKNOWN


def tokens(value):
    """Use Chinese character bigrams and whole Latin/number tokens, without embeddings."""
    value = normalize(value)
    result = set(re.findall(r"[a-z0-9]+", value))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        result.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return result


def text_score(query, document):
    """Return query-token coverage; a blank query has no relevance score."""
    query_tokens = tokens(query)
    return len(query_tokens & tokens(document)) / len(query_tokens) if query_tokens else 0.0


def descriptor_text(record):
    """Render image-derived appearance only; pathology does not affect phenotype recall."""
    features = record["features"]
    return " ".join(" ".join(value) if isinstance(value, list) else value
                    for value in features.values())


def render_lesion(record):
    """Render escaped, deterministic OLK/1 text for storage and model context."""
    fields = {"lesion_id": record["lesion_id"], **record["features"],
              "size": record["size_description"], "pathology": record["pathology"]}
    return "OLK/1;" + ";".join(key + "=" + json.dumps(value, ensure_ascii=False, sort_keys=True)
                               for key, value in fields.items())


def phenotype_score(query, record, weights):
    """Match supplied known fields; unknown stored values never count as matches."""
    total = 0.0
    matched = 0.0
    evidence = {}
    for field, weight in weights.items():
        if field == "text" or field not in query or not weight:
            continue
        value = query[field]
        if (isinstance(value, str) and not known(value)) or value == []:
            continue
        total += weight
        candidate = record["features"][field]
        if isinstance(value, list):
            wanted = {normalize(item) for item in value}
            actual = {normalize(item) for item in candidate}
            score = len(wanted & actual) / len(wanted | actual) if wanted | actual else 0.0
        else:
            score = float(known(candidate) and normalize(value) == normalize(candidate))
        evidence[field] = score
        matched += weight * score
    if "text" in query and known(query["text"]) and tokens(query["text"]):
        weight = weights.get("text", 1.0)
        total += weight
        evidence["text"] = text_score(query["text"], descriptor_text(record))
        matched += weight * evidence["text"]
    return (matched / total if total else None), evidence


def associated(record, lesion):
    """Explicit lesion links span visits; unlinked records apply only to their encounter."""
    links = record.get("lesion_ids", [])
    return lesion["lesion_id"] in links if links else record["encounter_id"] == lesion["encounter_id"]


def rank_text(records, query, limit, settings=None):
    """Return positive text matches, or recent writes for an explicitly empty query."""
    ranked = [{"record": record, "score": text_score(query, record["search_text"])}
              for record in records]
    if query.strip():
        ranked = [item for item in ranked if item["score"] > 0]
    ranked.sort(key=lambda item: (-item["score"], -item["record"]["updated_order"],
                                  item["record"]["record_id"]))
    if settings and query.strip():
        return select_ties(ranked, limit, ["score"], "record", settings)
    return ranked[:limit]


def hierarchical(records, query, settings):
    """Recall descriptors first and use care relevance only as the second sort key."""
    excluded = set(query.get("exclude_record_ids", []))
    filtered = {category: [record for record in items if record["record_id"] not in excluded]
                for category, items in records.items()}
    lesions = filtered["lesion"]
    if "encounter_id" in query:
        lesions = [record for record in lesions if record["encounter_id"] == query["encounter_id"]]
    candidates = []
    has_query = phenotype_score(query["lesion_query"], {"features": {
        name: [] if name == "tooth_positions" else "unknown" for name in settings["weights"] if name != "text"
    }}, settings["weights"])[0] is not None
    if not has_query:
        return {"status": "needs_clarification", "reason": "No known phenotype query; supply appearance or location.",
                "candidates": [], "supplementary": {"experience": [], "doctor": []},
                "ranking": ["phenotype_score DESC", "care_score DESC", "utility/frequency sampling within equal relevance"]}
    for record in lesions:
        score, evidence = phenotype_score(query["lesion_query"], record, settings["weights"])
        if score is not None and score > 0 and score >= settings["min_phenotype_score"]:
            candidates.append({"lesion": record, "phenotype_score": score, "field_matches": evidence})
    candidates.sort(key=lambda item: (-item["phenotype_score"], -item["lesion"]["updated_order"],
                                     item["lesion"]["record_id"]))
    count = len(candidates)
    candidates = select_ties(candidates, settings["recall_limit"], ["phenotype_score"], "lesion", settings)
    for item in candidates:
        care = [record for record in filtered["care"] if associated(record, item["lesion"])]
        ranked = rank_text(care, query["care_query"], settings["per_category_limit"], settings)
        item["care"] = ranked
        item["care_score"] = ranked[0]["score"] if ranked else 0.0
    candidates.sort(key=lambda item: (-item["phenotype_score"], -item["care_score"],
                                     -item["lesion"]["updated_order"], item["lesion"]["record_id"]))
    selected = select_ties(candidates, query["top_k"], ["phenotype_score", "care_score"], "lesion", settings)
    supplementary = {}
    for category in ("experience", "doctor"):
        items = [record for record in filtered[category]
                 if any(associated(record, item["lesion"]) for item in selected)]
        supplementary[category] = rank_text(items, query[category + "_query"], settings["per_category_limit"], settings)
    return {"status": "ok" if selected else "no_match", "candidates": selected,
            "supplementary": supplementary, "stage1_match_count": count,
            "stage1_truncated": count > settings["recall_limit"],
            "ranking": ["phenotype_score DESC", "care_score DESC", "utility/frequency sampling within equal relevance"],
            "algorithm": "structured field matching + lexical token coverage; no embedding model"}
