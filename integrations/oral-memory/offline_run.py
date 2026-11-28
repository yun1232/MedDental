"""Shared Python train/test entry: deterministic splits and atomic memory checkpoints."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from batch_data import fingerprint, public_case, read_cases, split_cases, treatment_metrics
from model_client import ModelClient, load_env
from offline_memory import OfflineMemory
from vision_loop import run_episode

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def backup(source, target):
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')


@contextlib.contextmanager
def lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'run.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(f'{path} is already owned; check the previous process before removing a stale lock') from None
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def cause_category(value):
    for word in ('氟斑', '龋性', '发育性', 'MIH', '矿化不全'):
        if word.lower() in value.lower():
            return 'developmental' if word in ('发育性', 'MIH', '矿化不全') else word
    return re.sub(r'\s', '', value).lower()


def metrics(prediction, labels):
    return {'etiology_category_match': bool(prediction['etiology']) and cause_category(prediction['etiology']) == cause_category(labels['病因']),
            'etiology_full_text_match': re.sub(r'\s', '', prediction['etiology']) == re.sub(r'\s', '', labels['病因']),
            'treatment': treatment_metrics(prediction['treatment'], labels['治疗方案'])}


def prepare(args):
    rows, excluded, headers = read_cases(args.workbook, args.images, args.sheet)
    data_hash = fingerprint({'workbook': hashlib.sha256(args.workbook.read_bytes()).hexdigest(),
                             'images': {c['id']: [i['sha256'] for i in c['images']] for c in rows}})
    split = {'version': 3, 'dataset_hash': data_hash, 'seed': args.seed, 'test_ratio': args.test_ratio,
             'split': split_cases(rows, args.seed, args.test_ratio), 'excluded': excluded, 'headers': headers}
    path = args.output / 'split.json'
    if path.exists() and read(path) != split:
        raise ValueError('Dataset or split changed; choose a fresh output directory')
    save(path, split)
    implementation = fingerprint({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in Path(__file__).parent.glob('*.py')})
    config = {'version': 3, 'dataset_hash': data_hash, 'input_mode': args.input_mode, 'model': args.model,
              'endpoint_hash': fingerprint(args.base_url), 'max_rounds': args.max_rounds,
              'confidence_threshold': args.confidence_threshold, 'top_k': args.top_k,
              'implementation': implementation}
    config['hash'] = fingerprint(config)
    path = args.output / args.input_mode / 'config.json'
    if path.exists() and read(path) != config:
        raise ValueError('Input mode or pipeline configuration changed; choose a fresh output directory')
    save(path, config)
    print(json.dumps({'valid_cases': len(rows), 'train': len(split['split']['train']),
                      'test': len(split['split']['test']), 'input_mode': args.input_mode, 'excluded': excluded}, ensure_ascii=False), flush=True)
    return {r['id']: r for r in rows}, split, config


def run_phase(phase, args, cases, split, config, client):
    root = args.output / args.input_mode
    pointer = root / 'memory/current.json'
    current = read(pointer) if pointer.exists() else None
    if phase == 'test' and not current:
        raise ValueError('Test requires the training memory: run train.py first with the same input mode')
    frozen = Path(current['snapshot']) if current else None
    frozen_hash = hashlib.sha256(frozen.read_bytes()).hexdigest() if frozen else None
    if phase == 'test':
        if current['config_hash'] != config['hash']:
            raise ValueError('Training memory and evaluation config differ')
        stamp = root / 'test/frozen.json'
        marker = {'snapshot': str(frozen), 'sha256': frozen_hash}
        if stamp.exists() and read(stamp) != marker:
            raise ValueError('Evaluation is already bound to another training snapshot')
        save(stamp, marker)
    ids = split['split'][phase][:args.limit] if args.limit else split['split'][phase]
    results = []
    for index, cid in enumerate(ids):
        receipt = root / phase / f'case-{cid}.json'
        if phase == 'train' and not receipt.exists() and pointer.exists():
            committed = read(pointer)
            if committed['case_id'] == cid:
                save(receipt, read(committed['result']))
        if receipt.exists():
            value = read(receipt)
            if value['config_hash'] != config['hash']:
                raise ValueError('Result belongs to another configuration')
            results.append(value)
            continue
        work = root / phase / f'case-{cid}-{uuid.uuid4().hex[:10]}'
        work.mkdir(parents=True)
        visible = public_case(cases[cid], args.input_mode)
        save(work / 'input.json', visible)
        started = time.monotonic()
        memory = None
        try:
            if phase == 'train':
                db = work / 'memory.sqlite3'
                if pointer.exists():
                    backup(read(pointer)['snapshot'], db)
                memory = OfflineMemory(db, writable=True)
            else:
                memory = OfflineMemory(frozen, writable=False)
            episode = run_episode(visible, client, memory, work, args.max_rounds, args.confidence_threshold, args.top_k)
            save(work / 'episode.json', episode)
            prediction = {'etiology': episode['final']['etiology'], 'treatment': episode['final']['treatment']}
            value = {'case_id': cid, 'phase': phase, 'input_mode': args.input_mode, 'status': episode['status'],
                     'rounds': episode['rounds'], 'prediction': prediction, 'labels': cases[cid]['labels'],
                     'uncertainties': episode['final']['uncertainties'], 'metrics': metrics(prediction, cases[cid]['labels']),
                     'seconds': round(time.monotonic() - started, 3), 'config_hash': config['hash'], 'work': str(work)}
            if phase == 'train':
                value['memory_update'] = memory.remember({**visible, 'labels': cases[cid]['labels']}, episode, split['dataset_hash'])
                memory.close()
                memory = None
                snapshot = root / 'memory' / f'after-{cid}-{uuid.uuid4().hex[:10]}.sqlite3'
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                backup(db, snapshot)
                result_path = snapshot.with_suffix('.json')
                save(result_path, value)
                save(pointer, {'snapshot': str(snapshot), 'case_id': cid, 'result': str(result_path),
                               'config_hash': config['hash']})
            save(receipt, value)
            results.append(value)
            print(f'{phase}/{args.input_mode} {index+1}/{len(ids)} case={cid} status={episode["status"]} rounds={episode["rounds"]}', flush=True)
        except Exception as exc:
            save(work / 'failure.json', {'case_id': cid, 'error': str(exc)})
            raise
        finally:
            if memory:
                memory.close()
        if phase == 'test' and hashlib.sha256(frozen.read_bytes()).hexdigest() != frozen_hash:
            raise RuntimeError('Frozen training memory changed during testing')
    n = len(results)
    summary = {'phase': phase, 'input_mode': args.input_mode, 'count': n,
               'planned_count': len(split['split'][phase]), 'partial': n < len(split['split'][phase]),
               'etiology_category_accuracy': sum(v['metrics']['etiology_category_match'] for v in results) / n if n else None,
               'treatment_exact_match': sum(v['metrics']['treatment']['normalized_exact_match'] for v in results) / n if n else None,
               'treatment_component_f1': sum(v['metrics']['treatment']['component_f1'] for v in results) / n if n else None,
               'unresolved_rate': sum(v['status'] != 'resolved' for v in results) / n if n else None,
               'mean_rounds': sum(v['rounds'] for v in results) / n if n else None,
               'frozen_memory_sha256': frozen_hash if phase == 'test' else None,
               'note': 'Reference-label agreement only; not clinical correctness or real treatment success.'}
    save(root / phase / 'summary.json', summary)
    return summary


def main(phase, argv=None):
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument('--env-file', type=Path, default=ROOT / '.env.oral')
    selected, _ = env_parser.parse_known_args(argv)
    load_env(selected.env_file)
    parser = argparse.ArgumentParser(description=f'{phase}: offline image/text visual-refinement pipeline', parents=[env_parser])
    parser.add_argument('--input-mode', choices=['history', 'clinical'], default='history')
    parser.add_argument('--workbook', type=Path, default=ROOT / 'data/label.xlsx')
    parser.add_argument('--images', type=Path, default=ROOT / 'data/pic')
    parser.add_argument('--sheet')
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/vision')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--test-ratio', type=float, default=0.2)
    parser.add_argument('--max-rounds', type=int, default=4)
    parser.add_argument('--confidence-threshold', type=float, default=0.75)
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--base-url', default=os.environ.get('ORAL_MODEL_BASE_URL', os.environ.get('ORAL_BATCH_BASE_URL', 'http://127.0.0.1:8000/v1')))
    parser.add_argument('--model', default=os.environ.get('ORAL_MODEL_NAME', os.environ.get('ORAL_BATCH_MODEL', 'Qwen3-VL-8B-Instruct')))
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args(argv)
    if not 1 <= args.max_rounds <= 12 or not 0 < args.confidence_threshold <= 1 or not 1 <= args.top_k <= 12 or args.timeout <= 0 or (args.limit is not None and args.limit < 1):
        parser.error('Invalid round, confidence, top-k, timeout or limit setting')
    args.output, args.workbook, args.images = args.output.resolve(), args.workbook.resolve(), args.images.resolve()
    try:
        with lock(args.output):
            cases, split, config = prepare(args)
            if args.dry_run:
                return 0
            client = ModelClient(args.base_url, args.model, os.environ.get('ORAL_MODEL_API_KEY', os.environ.get('ORAL_BATCH_API_KEY', '')), args.timeout)
            run_phase(phase, args, cases, split, config, client)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
