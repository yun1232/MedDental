"""Four separate agents with a bounded evidence-driven offline state machine."""
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps
from batch_data import INPUT_FIELDS
from agents import (VISUAL, RETRIEVAL, REASON, AUDIT, UTILITY, fields,
                    visual_result, retrieval_result, reasoning_result, audit_result)


def make_view(originals, action, directory, step):
    index = action['image_index']
    with Image.open(originals[index]['path']) as image:
        x1, y1, x2, y2 = action['box']
        pixels = [int(x1 * image.width / 1000), int(y1 * image.height / 1000),
                  math.ceil(x2 * image.width / 1000), math.ceil(y2 * image.height / 1000)]
        if pixels[2] - pixels[0] < 2 or pixels[3] - pixels[1] < 2:
            raise ValueError('Selected region must cover at least 2x2 pixels')
        result = image.crop(pixels) if action['type'] == 'crop' else image.copy()
        if action['type'] == 'mask':
            ImageDraw.Draw(result).rectangle((pixels[0], pixels[1], pixels[2] - 1, pixels[3] - 1), fill=(127, 127, 127))
        path = Path(directory) / f'view-{step}-{action["type"]}.png'
        result.save(path, format='PNG')
    return {'id': 'view-' + str(step), 'kind': action['type'], 'path': str(path), 'image_index': index,
            'box': action['box'], 'pixel_box': pixels, 'reason': action['reason'],
            'memory_ids': action['memory_ids'], 'inference_step': step}


def findings(visual):
    return list(dict.fromkeys(item['finding'] for item in visual['observations']))


def next_route(reasoning, audit, visual):
    # An approval cannot suppress a visual gap raised by the reasoning/visual agent.
    kinds = {u['kind'] for u in reasoning['uncertainties']}
    if audit['decision'] == 'visual' or 'visual' in kinds or visual['conflicts'] or visual['uncertainties']:
        return 'visual'
    if audit['decision'] == 'unresolved':
        return 'unresolved'
    if audit['decision'] == 'retrieve' or 'memory' in kinds:
        return 'retrieve'
    if audit['decision'] == 'reasoning' or 'reasoning' in kinds:
        return 'reasoning'
    return 'finish'


def run_episode(case, client, memory, directory, max_rounds=4, threshold=0.75, top_k=5):
    if set(case) != {'id', 'text', 'images'}:
        raise ValueError('Only the visible case may enter the inference loop; labels are forbidden')
    if not isinstance(case['text'], dict) or set(case['text']) not in [set(v) for v in INPUT_FIELDS.values()]:
        raise ValueError('Patient text must match the history or clinical field allowlist exactly')
    if not 1 <= len(case['images']) <= 3 or not 1 <= max_rounds <= 12:
        raise ValueError('Use one to three originals and one to twelve inference rounds')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    originals = []
    for index, image in enumerate(case['images']):
        path = directory / f'original-{index}.png'
        with Image.open(image['path']) as source:
            normalized = ImageOps.exif_transpose(source).convert('RGB')
            if normalized.width * normalized.height > 20_000_000:
                raise ValueError('Image exceeds 20 megapixels')
            normalized.save(path, format='PNG')
        originals.append({'id': 'original-' + str(index), 'kind': 'original', 'path': str(path), 'image_index': index})
    steps, views, visual_passes, retrievals = [], [], [], []
    seen, known_findings, stale = set(), set(), 0
    context, current_visual, result, audit = [], None, None, None

    def current_images():
        return [*originals, *([{k: v for k, v in views[-1].items() if k != 'memory_ids'}] if views else [])]

    def inspect(step, questions):
        nonlocal current_visual, known_findings, stale
        images = current_images()
        analysis = visual_result(client.complete('visual', VISUAL,
            {'patient_text': case['text'], 'previous_visual': current_visual,
             'focus_questions': questions, 'round': step + 1}, images), {i['id'] for i in images})
        new = set(findings(analysis)) - known_findings
        stale = stale + 1 if step and not new else 0
        known_findings.update(findings(analysis))
        current_visual = analysis
        visual_passes.append({'step': step, 'input_view': views[-1]['id'] if views else None, 'result': analysis})
        return analysis

    def retrieve(task, questions, step):
        # Exactly one native candidate pool regardless of the caller or current route.
        packets = memory.retrieve(case, findings(current_visual), gaps=questions, top_k=top_k)
        memory.delivered(packets, case['id'])
        visible = [{k: v for k, v in p.items() if k != '_images'} for p in packets]
        images = current_images()
        for packet in packets[:2]:
            for index, prior in enumerate(packet['_images']):
                if Path(prior['path']).is_file():
                    path = directory / f'historical-{packet["case_id"]}-{index}.png'
                    with Image.open(prior['path']) as source:
                        ImageOps.exif_transpose(source).convert('RGB').save(path, format='PNG')
                    images.append({'id': f'historical-{packet["case_id"]}-{index}',
                                   'kind': 'historical_reference', 'case_id': packet['case_id'],
                                   'image_index': index, 'path': str(path)})
        response = client.complete('retrieval', RETRIEVAL,
            {'task': task, 'patient_text': case['text'], 'visual_analysis': current_visual,
             'reasoning': result, 'review': audit, 'questions': questions, 'candidates': visible,
             'round': step + 1}, images)
        related, action = retrieval_result(response, packets, len(originals), task)
        retrievals.append({'step': step, 'task': task, 'questions': questions,
                           'candidate_ids': [p['case_id'] for p in packets], 'related_cases': related, 'action': action})
        return related, action

    baseline = inspect(0, [])
    context, _ = retrieve('context', [], 0)
    stop, blockers = 'round_budget', []
    for step in range(max_rounds):
        images = current_images()
        result = reasoning_result(client.complete('reasoning', REASON,
            {'patient_text': case['text'], 'visual_analysis': current_visual, 'related_cases': context,
             'previous_reasoning': result, 'review_feedback': audit, 'round': step + 1,
             'remaining_rounds': max_rounds - step - 1}, images),
             {i['id'] for i in images} | {p['case_id'] for p in context})
        audit = audit_result(client.complete('review', AUDIT,
            {'patient_text': case['text'], 'visual_analysis': current_visual, 'related_cases': context,
             'reasoning': result, 'round': step + 1}, images))
        route = next_route(result, audit, current_visual)
        steps.append({'step': step, 'input_view': views[-1]['id'] if views else None,
                      'visual_pass': len(visual_passes) - 1, 'retrieval_pass': len(retrievals) - 1,
                      'delivered_memories': [p['case_id'] for p in context],
                      'result': result, 'review': audit, 'route': route})
        if route == 'finish':
            stop = ('resolved' if audit['decision'] == 'approve' and not result['uncertainties']
                    and result['confidence'] >= threshold and result['etiology'].strip()
                    and result['treatment'].strip() else 'unresolved')
            break
        if route == 'unresolved':
            blockers.append(audit['reason'])
            stop = 'unresolved'
            break
        if step + 1 == max_rounds:
            break
        questions = list(dict.fromkeys([*(u['question'] for u in result['uncertainties']),
                                       *audit['requests'], *audit['conflicts'], *current_visual['conflicts'],
                                       *current_visual['uncertainties']]))
        if route == 'visual':
            if stale >= 2:
                stop = 'no_new_evidence'
                break
            context, action = retrieve('visual', questions, step + 1)
            if action['type'] == 'none':
                blockers.append(action['reason'])
                stop = 'unresolved'
                break
            view = make_view(originals, action, directory, step + 1)
            signature = (view['kind'], view['image_index'], tuple(view['pixel_box']))
            if signature in seen:
                Path(view['path']).unlink()
                stop = 'repeated_view'
                break
            seen.add(signature)
            views.append(view)
            inspect(step + 1, questions)
            # Updated visual evidence also updates case applicability before reasoning.
            context, _ = retrieve('context', questions, step + 1)
        elif route == 'retrieve':
            context, _ = retrieve('context', questions, step + 1)
        # A reasoning-only correction reuses current visual evidence and case context.
    final = {**result, 'observations': findings(current_visual),
             'uncertainty_details': result['uncertainties'],
             'uncertainties': list(dict.fromkeys([*(u['question'] for u in result['uncertainties']),
                                                  *current_visual['conflicts'], *current_visual['uncertainties'], *blockers,
                                                  *(audit['conflicts'] if audit['decision'] != 'approve' else []),
                                                  *(audit['requests'] if audit['decision'] != 'approve' else [])]))}
    if stop != 'resolved' and not final['uncertainties']:
        final['uncertainties'] = ['未达到收敛条件：' + stop]
    episode = {'case_id': case['id'], 'status': stop, 'baseline': baseline, 'steps': steps,
               'visual_passes': visual_passes, 'retrievals': retrievals, 'views': views,
               'final': final, 'rounds': len(steps), 'memory_assessments': []}
    used = {cid for view in views for cid in view['memory_ids']}
    if memory.writable and used:
        # Deliberately exclude predictions, audit verdicts and all labels from this context.
        payload = {'initial_observation': baseline,
                   'regions': [{k: v for k, v in view.items() if k != 'path'} for view in views],
                   'region_judgments': [p for p in visual_passes if p['input_view']],
                   'used_memory_ids': sorted(used)}
        assessment = client.complete('memory_review', UTILITY, payload,
            [*originals, *[{k: v for k, v in view.items() if k != 'memory_ids'} for view in views]])
        fields(assessment, 'assessments', 'memory review')
        rows = assessment['assessments']
        if not isinstance(rows, list) or any(not isinstance(a, dict) or not isinstance(a.get('case_id'), str) for a in rows):
            raise ValueError('Invalid memory utility assessments')
        if {a['case_id'] for a in rows} != used or len(rows) != len(used):
            raise ValueError('Visual memory review must cover exactly the actually used cases')
        for item in rows:
            fields(item, 'case_id verdict reason steps', 'visual memory assessment')
            if item['verdict'] not in ('helpful', 'not_helpful', 'uncertain') or not isinstance(item['reason'], str) or not item['reason'].strip():
                raise ValueError('Invalid visual memory assessment')
            allowed = {v['inference_step'] for v in views if item['case_id'] in v['memory_ids']}
            if not isinstance(item['steps'], list) or not item['steps'] or any(type(n) is not int or n not in allowed for n in item['steps']):
                raise ValueError('Memory assessment references an untested region')
        # Validate the entire review before applying any feedback.
        for item in rows:
            memory.feedback(case['id'], item['case_id'], item['verdict'], item['reason'])
        episode['memory_assessments'] = rows
    return episode
