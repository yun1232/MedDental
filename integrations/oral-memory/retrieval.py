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
    base = " ".join(" ".join(value) if isinstance(value, list) else value
                    for value in features.values())
    grounded = [finding["description"] + " " + " ".join(finding["tags"])
                for link in record.get("visual", []) if not link.get("stale")
                for finding in link["findings"] if finding["certainty"] == "clear"]
    return " ".join([base, record.get("visual_description", ""), *grounded])


def render_lesion(record):
    """Render escaped, deterministic OLK/1 text for storage and model context."""
    fields = {"lesion_id": record["lesion_id"], **record["features"],
              "size": record["size_description"], "pathology": record["pathology"]}
    if record.get("visual_description"):
        fields["visual_description"] = record["visual_description"]
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
    if record.get("patient_id") != lesion.get("patient_id"):
        return False
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


def staged(records, query, settings, purpose):
    """Phase-specific retrieval on the same records: history-only, then phenotype+history."""
    from patient_experience import normalize as as_experience
    from extensions import reference
    excluded = set(query.get('exclude_record_ids', []))
    filtered = {category: [r for r in values if r['record_id'] not in excluded] for category, values in records.items()}
    experiences = [*filtered['experience'], *filtered['care']]
    history_query = query.get('patient_experience_query', '')
    has_history = bool(known(history_query) and tokens(history_query))
    # Visual-stage code never calls phenotype_score, even if a caller supplies descriptors.
    has_phenotype = purpose == 'diagnosis' and phenotype_score(query['lesion_query'], {'features': {
        name: [] if name == 'tooth_positions' else 'unknown' for name in settings['weights'] if name != 'text'
    }}, settings['weights'])[0] is not None
    channels = ['patient_experience'] if purpose == 'visual' else ['lesion', 'patient_experience']
    order = ['patient_experience_score DESC'] if purpose == 'visual' else ['phenotype_score DESC', 'patient_experience_score DESC']
    result = {'candidates': [], 'supplementary': {'patient_experience': [], 'doctor': []},
              'retrieval_channels': channels, 'ranking': [*order, 'model M * iteration D follows initial retrieval'],
              'algorithm': 'shared memory fields and lexical matching with phase-specific query channels'}
    if not has_history and not has_phenotype:
        return {**result, 'status': 'needs_clarification', 'reason': 'Patient experience is required before visual retrieval' if purpose == 'visual' else 'No known phenotype or patient experience'}
    lesions = filtered['lesion']
    if 'encounter_id' in query:
        lesions = [r for r in lesions if r['encounter_id'] == query['encounter_id']]
    groups = [(lesion, [e for e in experiences if associated(e, lesion)]) for lesion in lesions]
    # Experience-only older cases stay usable; no fabricated phenotype record is required.
    orphans = {}
    for event in experiences:
        if any(associated(event, lesion) for lesion in lesions):
            continue
        if 'encounter_id' in query and event['encounter_id'] != query['encounter_id']:
            continue
        identity = (event.get('patient_id'), event['encounter_id'], tuple(sorted(event.get('lesion_ids', []))))
        orphans.setdefault(identity, []).append(event)
    groups.extend((max(events, key=lambda r: (r['updated_order'], r['category'], r['record_id'])), events) for events in orphans.values())
    candidates = []
    for anchor, events in groups:
        history_score = max((text_score(history_query, e['search_text']) for e in events), default=0) if has_history else 0
        score, matches = (phenotype_score(query['lesion_query'], anchor, settings['weights'])
                          if has_phenotype and anchor['category'] == 'lesion' else (0, {}))
        score = score or 0
        if purpose == 'visual' and history_score <= 0:
            continue
        if purpose == 'diagnosis' and history_score <= 0 and (score <= 0 or score < settings['min_phenotype_score']):
            continue
        event_context = sorted([{'record': as_experience(e), 'score': text_score(history_query, e['search_text']) if has_history else 0} for e in events],
                               key=lambda x: (-x['score'], -x['record']['updated_order']))[:settings['per_category_limit']]
        care_context = [{'record': e, 'score': text_score(history_query, e['search_text']) if has_history else 0} for e in events if e['category'] == 'care']
        candidates.append({'lesion': anchor, 'phenotype_score': score, 'field_matches': matches,
                           'patient_experience': event_context, 'patient_experience_score': history_score,
                           'care': care_context[:settings['per_category_limit']],
                           'care_score': max((c['score'] for c in care_context), default=0)})
    keys = ['patient_experience_score'] if purpose == 'visual' else ['phenotype_score', 'patient_experience_score']
    candidates.sort(key=lambda x: (*(-x[k] for k in keys), -x['lesion']['updated_order'], x['lesion']['record_id']))
    matched = len(candidates)
    initial_settings = {**settings, 'initial_only': True}
    selected = select_ties(candidates[:settings['recall_limit']], query['top_k'], keys, 'lesion', initial_settings)
    events = {}
    for c in selected:
        for event in c['patient_experience']:
            ref = reference(event['record'])
            events[(event['record'].get('patient_id'), ref['category'], ref['record_id'], ref['version'])] = event
    # Doctor notes are context after selection and never a visual recall channel.
    notes = [n for n in filtered['doctor'] if any(associated(n, c['lesion']) for c in selected if c['lesion']['category'] == 'lesion')]
    return {**result, 'status': 'ok' if selected else 'no_match', 'candidates': selected,
            'supplementary': {'patient_experience': list(events.values()), 'doctor': rank_text(notes, '', settings['per_category_limit'])},
            'stage1_match_count': matched, 'stage1_truncated': matched > settings['recall_limit']}


def unified(records, query, settings):
    """One candidate pool for offline visual evidence, case reasoning and region guidance.

    Reuse native descriptor/history matching and decay. Care/target records never
    become history recall text; there is no phase-specific channel switch.
    """
    candidates = []
    for lesion in records['lesion']:
        appearance, matches = phenotype_score(query['lesion_query'], lesion, settings['weights'])
        appearance = appearance or 0
        history = max((text_score(query['patient_experience_query'], r['search_text'])
                       for r in records['experience'] if associated(r, lesion)), default=0)
        if history <= 0 and (appearance <= 0 or appearance < settings['min_phenotype_score']):
            continue
        score = appearance + 0.1 * history
        candidates.append({'lesion': lesion, 'matching_score': score, 'field_matches': matches,
                           'phenotype_score': appearance, 'patient_experience_score': history,
                           'retrieval_score': score * lesion['stats']['decay_score']})
    candidates.sort(key=lambda item: (-item['retrieval_score'], item['lesion']['case_id']))
    return candidates[:query['top_k']]
