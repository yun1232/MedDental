"""Closed, bounded contracts shared by the Harness team and deterministic store."""
from schemas import obj, text, choice, strings, MemoryError, validate, QUERY_FEATURES, REFERENCE


def array(item, maximum=20):
    return {"type": "array", "items": item, "maxItems": maximum}


UNIT = {"type": "integer", "minimum": 0, "maximum": 1000}
BOX = array(UNIT, 4)  # additionally require exactly four ordered coordinates
CASE_REF = obj({"case_id": text(), "version": {"type": "integer", "minimum": 1}}, ("case_id", "version"))
REVIEW_STEP = {"type": "integer", "minimum": 1, "maximum": 24}
VISUAL_VALUE_REVIEW = obj({"summary": text(), "assessments": array(obj({
    **CASE_REF['properties'], "verdict": choice("new_information", "no_new_information", "uncertain"),
    "review_steps": array(REVIEW_STEP, 24), "finding_refs": array(obj({"step": REVIEW_STEP,
        "finding_id": text()}, ("step", "finding_id")), 30), "reason": text()},
    ("case_id", "version", "verdict", "review_steps", "finding_refs", "reason")), 12)},
    ("summary", "assessments"))
CONTROL = obj({"action": choice("ask", "visual", "retrieve", "reasoning", "finish", "human"),
    "question": text(empty=True), "reason": text(), "gaps": array(obj({"field": text(), "why": text(),
    "case_refs": array(CASE_REF, 12)}, ("field", "why", "case_refs")), 10)}, ("action", "question", "reason", "gaps"))
LESSON = obj({"condition": text(), "mistake": text(), "correction": text(), "action": choice("ask", "visual", "retrieve", "reasoning", "review"),
    "limits": text()}, ("condition", "mistake", "correction", "action", "limits"))
CONTRIBUTION = obj({"case_id": text(), "version": {"type": "integer", "minimum": 1}, "view_id": text(), "finding_id": text()},
    ("case_id", "version", "view_id", "finding_id"))
REGION = obj({"image_id": text(), "box": BOX, "description": text(), "basis": strings(), "source_cases": array(CASE_REF, 12)},
             ("image_id", "box", "description", "basis"))
FINDING = obj({"id": text(), "image_id": text(), "description": text(),
               "supporting_views": strings(), "certainty": choice("clear", "uncertain"),
               "tags": strings()}, ("id", "image_id", "description", "supporting_views", "certainty", "tags"))
RANK = obj({"case_id": text(), "version": {"type": "integer", "minimum": 1},
            "relevance": {"type": "integer", "minimum": 0, "maximum": 100},
            "supports": strings(), "contradictions": strings(), "reason": text()},
           ("case_id", "version", "relevance", "supports", "contradictions", "reason"))
HYPOTHESIS = obj({"name": text(), "support_ids": strings(), "against_ids": strings(),
                  "missing": strings()}, ("name", "support_ids", "against_ids", "missing"))
OUTPUTS = {
    "patient": obj({"summary": text(), "phenotype": strings(), "history": strings(),
                    "missing": strings(), "next_question": text(empty=True), "red_flags": strings(),
                    "patient_experiences": strings(), "features": QUERY_FEATURES, "image_bindings": array(obj({"image_id": text(), "reference": REFERENCE}, ("image_id", "reference")), 3)},
                   ("summary", "phenotype", "history", "missing", "next_question", "red_flags")),
    "retrieval": obj({"ranking": array(RANK, 72), "transferable_lessons": strings(), "limitations": strings()},
                     ("ranking", "transferable_lessons", "limitations")),
    "visual": obj({"findings": array(FINDING, 30), "regions": array(REGION, 2),
                   "quality_issues": strings(), "mask_sensitivity": strings(), "contributions": array(CONTRIBUTION, 30)},
                  ("findings", "regions", "quality_issues", "mask_sensitivity")),
    "reasoning": obj({"summary": text(), "hypotheses": array(HYPOTHESIS, 8), "case_refs": array(
        obj({"case_id": text(), "version": {"type": "integer", "minimum": 1}}, ("case_id", "version")), 12),
        "next_steps": strings(), "uncertainties": strings(), "red_flags": strings()},
        ("summary", "hypotheses", "case_refs", "next_steps", "uncertainties", "red_flags")),
    "review": obj({"route": choice("approve", "patient", "visual", "retrieval", "reasoning", "human"),
                   "issues": strings(), "required_changes": text(empty=True), "lesson_drafts": array(LESSON, 5)},
                  ("route", "issues", "required_changes")),
}


def check_output(role, value):
    validate(value, OUTPUTS[role])
    if role == "visual":
        for region in value["regions"]:
            b = region["box"]
            if len(b) != 4 or not (b[0] < b[2] and b[1] < b[3]):
                raise MemoryError("invalid_region", "Use ordered normalized [x1,y1,x2,y2] in 0..1000")
    return value
