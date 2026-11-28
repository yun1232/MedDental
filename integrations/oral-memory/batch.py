"""Reproducible memory-learning / frozen-evaluation orchestration; no model calls here."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from batch_data import fingerprint, public_case, read_cases, split_cases, treatment_metrics

ROOT = Path(__file__).resolve().parents[2]


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def database_copy(source, destination):
    """SQLite backup includes committed WAL state; copying only the main file would not."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        raise ValueError('Missing memory snapshot')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            # A standalone snapshot must not create WAL/SHM files when read later.
            dst.execute('PRAGMA journal_mode=DELETE')


def tree_hash(path):
    path = Path(path)
    return fingerprint({str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(path.rglob('*')) if p.is_file()})


def seed_work(snapshot, work):
    work.mkdir(parents=True, exist_ok=False)
    if snapshot:
        database_copy(snapshot / 'shared.sqlite3', work / 'shared.sqlite3')
        if (snapshot / 'images' / 'shared').exists():
            shutil.copytree(snapshot / 'images' / 'shared', work / 'images' / 'shared')


def learn_case(case, episode, work, manifest):
    """Dataset-supervised teacher writeback AFTER prediction; never infer clinical success."""
    if case['id'] not in manifest['split']['train']:
        raise ValueError('Only training cases may update memory')
    if episode['status'] != 'awaiting_doctor':
        return {'learned': False, 'reason': 'team_not_approved'}
    from store import MemoryStore, settings_from_env
    from team_memory import MedicalProject
    store = MemoryStore(work / 'private.sqlite3', 'batch:' + case['id'], 'doctor', settings_from_env({'ORAL_MEMORY_CASE_DRIVEN': '1'}))
    project = None
    try:
        project = MedicalProject(store, shared_path=work / 'shared.sqlite3',
                                 asset_root=work / 'images', domain=manifest['domain'])
        run = project.review(episode['run_id'], episode['revision'], True,
                             note='离线训练集的医生标注监督；不代表真实患者治疗效果。',
                             outcome=case['reference_treatment'], success=None)
        visual = project.latest(run, 'visual') or {'regions': []}
        facts = case['facts']
        phenotype = [k + '：' + v for k, v in facts.items() if k != '既往史']
        publication = {
            'phenotype': phenotype, 'context': ['既往史：' + facts.get('既往史', '未记录')],
            'evidence': ['来源为离线训练表中的医生描述，模型区域仅作观察线索。'],
            'experience': '训练集参考治疗方案：' + case['reference_treatment'],
            'pitfalls': ['单病例参考方案，不代表可直接用于其他病例。'],
            'outcome': '真实治疗效果未提供',
            'source_ref': 'dataset:' + manifest['dataset_hash'] + ':case:' + case['id'],
            'image_ids': [i['image_id'] for i in run['input']['images']],
            'regions': visual['regions'],
        }
        published = project.publish(run['run_id'], 'train-' + case['id'], 0, True, True, publication)
        return {'learned': True, 'case_id': published['case_id'], 'version': published['version'],
                'supervision': 'dataset_label', 'clinical_success': None}
    finally:
        if project:
            project.close()
        store.close()


def commit_memory(work, memory, cid, manifest_hash, result):
    """Immutable generation + atomic pointer: interrupted episodes never partially update memory."""
    generation = memory / 'snapshots' / ('case-' + cid + '-' + uuid.uuid4().hex[:10])
    generation.mkdir(parents=True)
    database_copy(work / 'shared.sqlite3', generation / 'shared.sqlite3')
    if (work / 'images' / 'shared').exists():
        shutil.copytree(work / 'images' / 'shared', generation / 'images' / 'shared')
    save_json(generation / 'snapshot.json', {'case_id': cid, 'manifest_hash': manifest_hash})
    save_json(generation / 'result.json', result)
    save_json(memory / 'current.json', {'snapshot': str(generation.resolve()), 'case_id': cid,
                                      'manifest_hash': manifest_hash})
    return generation


def get_snapshot(memory):
    pointer = memory / 'current.json'
    return Path(read_json(pointer)['snapshot']) if pointer.exists() else None


def run_worker(case, mode, work, args):
    save_json(work / 'input.json', public_case(case, mode, args.max_turns))
    env = {**os.environ, 'ORAL_MEMORY_ROOT': str(ROOT), 'ORAL_MEMORY_CASE_DRIVEN': '1',
           'ORAL_MEMORY_DB': str(work / 'private.sqlite3'), 'ORAL_MEMORY_PATIENT_ID': 'batch:' + case['id'],
           'ORAL_MEMORY_ENCOUNTER_ID': 'batch-case-' + case['id'], 'ORAL_MEMORY_ACTOR_ROLE': 'patient',
           'ORAL_MEMORY_PYTHON': sys.executable, 'MEDICAL_SHARED_DB': str(work / 'shared.sqlite3'),
           'MEDICAL_ASSET_ROOT': str(work / 'images'), 'MEDICAL_DOMAIN': args.domain,
           'ORAL_BATCH_CASE_FILE': str(work / 'input.json'), 'ORAL_BATCH_RESULT_FILE': str(work / 'episode.json'),
           'ORAL_BATCH_HARNESS_HOME': str(work / 'harness'),
           'ORAL_BATCH_PROVIDER': args.provider, 'ORAL_BATCH_MODEL': args.model}
    env.pop('ORAL_DOCTOR_TOKEN', None)
    with (work / 'worker.log').open('w', encoding='utf-8') as log:
        child = subprocess.Popen([args.node, '--import', 'tsx/esm',
                                  'integrations/oral-memory/batch-worker.mjs'], cwd=ROOT, env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=os.name != 'nt',
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        try:
            code = child.wait(timeout=args.timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise RuntimeError('Episode timed out; inspect worker.log') from exc
    if code or not (work / 'episode.json').exists():
        raise RuntimeError(f'Worker exited {code}; inspect {work / "worker.log"}')
    episode = read_json(work / 'episode.json')
    if episode['case_id'] != case['id'] or episode['mode'] != mode:
        raise RuntimeError('Worker returned another case or mode')
    return episode


def prepare(args):
    cases, excluded, headers = read_cases(args.workbook, args.images, args.sheet)
    data_hash = fingerprint({'workbook': hashlib.sha256(args.workbook.read_bytes()).hexdigest(),
                             'images': {c['id']: [i['sha256'] for i in c['images']] for c in cases}})
    manifest = {'version': 1, 'dataset_hash': data_hash, 'seed': args.seed, 'test_ratio': args.test_ratio,
                'headers': headers, 'split': split_cases(cases, args.seed, args.test_ratio), 'excluded': excluded,
                'domain': args.domain, 'max_turns': args.max_turns, 'provider': args.provider, 'model': args.model}
    provider_patch = os.environ.get('ORAL_BATCH_PROVIDER_PATCH')
    manifest['provider_configuration_hash'] = fingerprint({
        'base_url': os.environ.get('ORAL_BATCH_BASE_URL') or 'http://127.0.0.1:8000/v1',
        'patch': hashlib.sha256((ROOT / provider_patch).read_bytes()).hexdigest() if provider_patch else None})
    manifest['pipeline_hash'] = fingerprint({name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ('batch.py', 'batch_data.py', 'batch-driver.mjs', 'batch-worker.mjs', 'batch-runtime.mjs',
                     'batch.cordis.yml', 'harness-team.mjs', 'harness-role.mjs', 'profiles/diseases.json')})
    manifest['manifest_hash'] = fingerprint(manifest)
    path = args.output / 'manifest.json'
    if path.exists() and read_json(path) != manifest:
        raise ValueError('Dataset, split or run configuration changed. Choose a new --output directory.')
    save_json(path, manifest)
    print(json.dumps({'labeled_cases': len(cases), 'train': len(manifest['split']['train']),
                      'test': len(manifest['split']['test']), 'excluded': excluded}, ensure_ascii=False), flush=True)
    return {c['id']: c for c in cases}, manifest


def phase_run(phase, mode, cases, manifest, args, worker=run_worker):
    directory = args.output / mode
    memory = directory / 'memory'
    destination = directory / phase
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = get_snapshot(memory)
    if phase == 'test' and snapshot is None:
        raise ValueError(f'{mode}: Train first; test requires an existing memory snapshot')
    if phase == 'test' and read_json(snapshot / 'snapshot.json')['manifest_hash'] != manifest['manifest_hash']:
        raise ValueError('Snapshot and split do not match')
    frozen_hash = tree_hash(snapshot) if phase == 'test' else None
    frozen_marker = destination / 'frozen-memory.json'
    if phase == 'test':
        marker = {'snapshot': str(snapshot), 'hash': frozen_hash}
        if frozen_marker.exists() and read_json(frozen_marker) != marker:
            raise ValueError('Test was started from another snapshot. Choose a fresh output directory.')
        save_json(frozen_marker, marker)
    ids = manifest['split'][phase]
    if args.limit:
        ids = ids[:args.limit]
    results = []
    for index, cid in enumerate(ids):
        completed = destination / ('case-' + cid + '.json')
        # Recover a crash between committing a train generation and writing its result receipt.
        current = get_snapshot(memory)
        recovery = current / 'result.json' if current and phase == 'train' else None
        if not completed.exists() and recovery and recovery.exists() and read_json(recovery)['case_id'] == cid:
            save_json(completed, read_json(recovery))
        if completed.exists():
            saved = read_json(completed)
            if saved.get('manifest_hash') != manifest['manifest_hash']:
                raise ValueError('Result receipt belongs to another run')
            results.append(saved)
            continue
        work = destination / ('case-' + cid + '-attempt-' + uuid.uuid4().hex[:10])
        seed_work(get_snapshot(memory) if phase == 'train' else snapshot, work)
        started = time.monotonic()
        try:
            episode = worker(cases[cid], mode, work, args)
            prediction = episode['proposal']['treatment_plan']
            result = {'case_id': cid, 'phase': phase, 'mode': mode, 'status': episode['status'],
                      'turns': episode['turns'], 'prediction': prediction,
                      'reference_treatment': cases[cid]['reference_treatment'],
                      'abstain': episode['proposal']['abstain'],
                      'metrics': treatment_metrics(prediction, cases[cid]['reference_treatment']),
                      'manifest_hash': manifest['manifest_hash'], 'work_directory': str(work),
                      'seconds': round(time.monotonic() - started, 3)}
            if phase == 'train':
                result['memory_update'] = learn_case(cases[cid], episode, work, manifest)
                # Prepare recovery receipt before publishing the immutable generation pointer.
                commit_memory(work, memory, cid, manifest['manifest_hash'], result)
            elif tree_hash(snapshot) != frozen_hash:
                raise RuntimeError('Frozen training memory was modified')
            save_json(completed, result)
            results.append(result)
            print(f'{mode}/{phase} {index+1}/{len(ids)} case={cid} status={result["status"]}', flush=True)
        except Exception as exc:
            save_json(work / 'failure.json', {'case_id': cid, 'error': str(exc), 'phase': phase})
            # Stop rather than skipping: changing training order would change the learned memory.
            raise
    count = len(results)
    summary = {'mode': mode, 'phase': phase, 'count': count,
               'planned_count': len(manifest['split'][phase]), 'partial': count < len(manifest['split'][phase]),
               'approved_count': sum(r['status'] == 'awaiting_doctor' for r in results),
               'abstention_rate': sum(r['abstain'] for r in results) / count if count else None,
               'normalized_exact_match': sum(r['metrics']['normalized_exact_match'] for r in results) / count if count else None,
               'mean_component_f1': sum(r['metrics']['component_f1'] for r in results) / count if count else None,
               'mean_turns': sum(r['turns'] for r in results) / count if count else None,
               'metric_note': 'Lexical treatment agreement only; not clinical correctness or treatment success.',
               'frozen_snapshot_hash': frozen_hash}
    save_json(destination / 'summary.json', summary)
    return summary


@contextlib.contextmanager
def output_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / 'batch.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(f'Another run may own {lock}; verify it has stopped before removing the stale lock') from None
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['prepare', 'train', 'test', 'all'], default='all')
    parser.add_argument('--mode', choices=['doctor', 'patient', 'both'], default='both')
    parser.add_argument('--workbook', type=Path, default=ROOT / 'data/label.xlsx')
    parser.add_argument('--images', type=Path, default=ROOT / 'data/pic')
    parser.add_argument('--sheet')
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/oral-batch')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--test-ratio', type=float, default=0.2)
    parser.add_argument('--max-turns', type=int, default=6)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--domain', default='enamel-white-spot')
    parser.add_argument('--provider', default=os.environ.get('ORAL_BATCH_PROVIDER', 'oral-vlm'))
    parser.add_argument('--model', default=os.environ.get('ORAL_BATCH_MODEL', 'Qwen3-VL-8B-Instruct'))
    parser.add_argument('--node', default='node')
    parser.add_argument('--dry-run', action='store_true', help='Validate data and write the split without model calls')
    args = parser.parse_args(argv)
    if not 1 <= args.max_turns <= 12 or args.timeout < 1 or (args.limit is not None and args.limit < 1):
        parser.error('Use max-turns 1..12, positive timeout and positive limit')
    args.output, args.workbook, args.images = args.output.resolve(), args.workbook.resolve(), args.images.resolve()
    return args


def main(argv=None):
    args = parse_args(argv)
    with output_lock(args.output):
        cases, manifest = prepare(args)
        if args.phase == 'prepare' or args.dry_run:
            return 0
        if not (ROOT / 'node_modules/tsx').exists():
            raise ValueError('Install workspace dependencies with pnpm install first')
        if args.provider == 'oral-vlm' and not os.environ.get('ORAL_BATCH_API_KEY'):
            raise ValueError('Set ORAL_BATCH_API_KEY in .env.oral (use a nonempty dummy value for a keyless local gateway)')
        modes = ['doctor', 'patient'] if args.mode == 'both' else [args.mode]
        phases = ['train', 'test'] if args.phase == 'all' else [args.phase]
        for mode in modes:
            for phase in phases:
                phase_run(phase, mode, cases, manifest, args)
    return 0


if __name__ == '__main__':
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Batch interrupted; completed cases are saved.', file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
