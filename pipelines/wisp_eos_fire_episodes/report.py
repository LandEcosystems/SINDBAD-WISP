"""Inspectable event-experiment cost report, including incomplete measurements.

Run against an isolated event root. Accounting is optional: --accounting queries
only registered Slurm job IDs. This module never submits jobs or deletes inputs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import subprocess
import time


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with temporary.open('w') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def seconds(value):
    """Slurm duration D-HH:MM:SS[.fraction], HH:MM:SS or MM:SS."""
    if value in (None, '', 'Unknown'):
        return None
    day, text = (value.split('-', 1) if '-' in value else ('0', value))
    parts = text.split(':')
    if len(parts) not in (2, 3):
        raise ValueError('Unrecognized Slurm duration: ' + value)
    result = float(day) * 86400 + sum(float(x) * 60**i for i, x in enumerate(reversed(parts)))
    if not math.isfinite(result) or result < 0:
        raise ValueError('Invalid Slurm duration: ' + value)
    return result


def epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Report timestamps need explicit timezone')
    return parsed.timestamp()


def jsonl(path):
    """An interrupted final append is reported, not mistaken for a complete record."""
    raw = Path(path).read_bytes()
    lines = raw.splitlines(keepends=True)
    result, incomplete = [], False
    for i, line in enumerate(lines):
        if i == len(lines) - 1 and not line.endswith(b'\n'):
            incomplete = True
            continue
        result.append(json.loads(line))
    return result, incomplete


def finite_sum(rows, name):
    values = [r[name] for r in rows if r.get(name) is not None]
    if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in values):
        raise ValueError('Invalid resource measurement: ' + name)
    return sum(values) if values else None


def maximum(rows, name):
    values = [r[name] for r in rows if r.get(name) is not None]
    return max(values) if values else None


def accounting(registrations):
    ids = sorted({str(r['job']) for r in registrations})
    if not ids:
        return {'status': 'pending', 'reason': 'No registered jobs', 'rows': []}
    command = ['sacct', '-X', '-n', '-P', '-j', ','.join(ids),
               '-o', 'JobIDRaw,State,ElapsedRaw,AllocCPUS,TotalCPU']
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.SubprocessError) as error:
        return {'status': 'unavailable', 'reason': str(error), 'rows': [], 'command': command}
    by_id = {str(r['job']): r for r in registrations}
    rows = {}
    for line in result.stdout.splitlines():
        values = line.split('|')
        if len(values) < 5 or values[0] not in by_id:
            continue
        job, state, elapsed, cpus, total_cpu = values[:5]
        row = dict(job=job, kind=by_id[job]['kind'], state=state,
                   elapsed_seconds=int(elapsed), allocated_cpus=int(cpus),
                   total_cpu_seconds=seconds(total_cpu))
        if job in rows and rows[job] != row:
            raise ValueError('Conflicting accounting rows: ' + job)
        rows[job] = row
    return {'status': 'available' if len(rows) == len(ids) else 'partial',
            'missing_jobs': [job for job in ids if job not in rows],
            'rows': list(rows.values()), 'command': command}


def summarize_accounting(raw):
    result = {}
    for kind in sorted({r['kind'] for r in raw['rows']}):
        rows = [r for r in raw['rows'] if r['kind'] == kind]
        result[kind] = {
            'jobs': len(rows), 'allocation_job_hours': sum(r['elapsed_seconds'] for r in rows) / 3600,
            'allocated_cpu_core_hours': sum(r['elapsed_seconds'] * r['allocated_cpus'] for r in rows) / 3600,
            'reported_cpu_seconds': finite_sum(rows, 'total_cpu_seconds'),
            'terminal_jobs': sum(r['state'].split()[0] in {
                'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL', 'PREEMPTED'} for r in rows),
        }
        if kind == 'gpu':
            result[kind]['allocated_gpu_hours_one_device_per_job'] = result[kind]['allocation_job_hours']
    return result


def baseline_progress(full_roots, tokens):
    """Count immutable receipts, not cached files that can disappear after cleanup."""
    evidence = {token: {'dates': set(), 'finals': [], 'active_roots': []} for token in tokens}
    for value in full_roots:
        root = Path(value)
        regions = read(root / 'manifests/regions.json', {})
        queue = read(root / 'state/queue.json', {})
        for token in tokens:
            if token not in regions:
                continue
            for path in sorted((root / 'manifests/tasks').glob(token + '_????-??/input_days.json')):
                evidence[token]['dates'].update(read(path, {}))
            region_id = regions[token]['region_id']
            receipt = read(root / 'reports/regions' / (region_id + '.json'))
            if receipt and receipt.get('validated'):
                evidence[token]['finals'].append({'root': str(root), 'path': receipt.get('path'),
                                                'sha256': receipt.get('sha256'), 'days': receipt.get('days')})
            if queue.get('active_cube') == token:
                evidence[token]['active_roots'].append({'root': str(root), 'phase': queue.get('phase')})
    result = {}
    for token, row in evidence.items():
        result[token] = {'completed_input_days_with_retained_receipts': len(row['dates']),
                         'full_expected_days': 3641, 'products': row['finals'], 'active_roots': row['active_roots'],
                         'status': 'reported complete' if row['finals'] else 'pending'}
    return result


def build_report(root, include_accounting=False, now_epoch=None):
    root = Path(root)
    now_epoch = time.time() if now_epoch is None else now_epoch
    selection = read(root / 'manifests/selection.json')
    queue = read(root / 'state/queue.json', {})
    operations = read(root / 'manifests/operations.json', {})
    if selection is None:
        raise ValueError('Frozen selection manifest is required')
    tasks = selection['tasks']
    tokens = sorted(selection['regions'])
    expected_days = sum(len(t['dates']) for t in tasks.values())
    expected = {(t['region'], day) for t in tasks.values() for day in t['dates']}
    if len(expected) != expected_days:
        raise ValueError('Selected dates are duplicated')
    # One file per job: snapshot updates overwrite that file rather than adding costs.
    resources = {}
    for path in sorted((root / 'state/job_resources').glob('*.json')):
        row = read(path)
        key = str(row['job'])
        if key in resources:
            raise ValueError('Duplicate job resource file: ' + key)
        resources[key] = row
    registrations = [read(p) for p in sorted((root / 'state/submissions').glob('*.json'))]
    events, incomplete = [], []
    for path in sorted((root / 'logs/worker_events').glob('*.jsonl')):
        rows, partial = jsonl(path)
        events.extend(rows)
        if partial:
            incomplete.append(str(path))
    cpu = [r for r in events if r['kind'] == 'cpu']
    gpu = [r for r in events if r['kind'] == 'gpu']
    # Every event is one actual execution, including retries. GPU events are
    # already per batch; never sum the duplicated timing in per-date receipts.
    receipts, receipt_days = [], set()
    future_checks, reference_checks = [], []
    for key, task in tasks.items():
        days = read(root / 'manifests/tasks' / key / 'input_days.json', {})
        for day, item in days.items():
            if (task['region'], day) not in expected:
                raise ValueError('Unselected input date in receipt: ' + day)
            receipt_days.add((task['region'], day))
            metadata = item.get('metadata', {})
            receipts.append(metadata)
            checks = metadata.get('frozen_checks', {})
            if checks:
                future_checks.append(checks.get('future_input_isolation', {}))
                reference_checks.append(checks.get('frozen_reference_parity', {}))
    resources_list = list(resources.values())
    measured_worker_seconds = finite_sum(resources_list, 'elapsed_seconds')
    observed_cpu_seconds = finite_sum(resources_list, 'cpu_seconds')
    starts = [epoch(r.get('at')) for r in registrations if r.get('at')]
    start = min(starts) if starts else None
    complete = epoch(queue.get('completed_at'))
    wall = max(0, (complete if complete is not None else now_epoch) - start) if start is not None else None
    account = accounting(registrations) if include_accounting else {
        'status': 'not requested', 'rows': [], 'reason': 'Pass --accounting on a Slurm login/compute node for allocated job hours'}
    selected_fraction = expected_days / (len(tokens) * 3641)
    raw = {
        'generated': dt.datetime.fromtimestamp(now_epoch, dt.timezone.utc).isoformat(),
        'phase': queue.get('phase', 'unknown'), 'completed': queue.get('phase') == 'COMPLETE',
        'date_selection': {
            'regions': {t: selection['regions'][t]['summary'] for t in tokens},
            'days': expected_days, 'tasks': len(tasks), 'full_comparator_days': len(tokens) * 3641,
            'selected_day_fraction': selected_fraction, 'avoided_day_fraction': 1 - selected_fraction,
            'selection_is_not_a_runtime_speedup_measurement': True,
        },
        'inputs': {
            'completed_days_with_final_chunk_receipts': len(receipt_days),
            'pending_days_without_final_chunk_receipts': expected_days - len(receipt_days),
            'recorded_read_seconds_final_receipts': finite_sum(receipts, 'read_seconds'),
            'recorded_assembly_seconds_final_receipts': finite_sum(receipts, 'assemble_seconds'),
            'complete_chunk_first_days_with_future_isolation_checks': len(future_checks),
            'future_isolation_passed': all(bool(c) and all(c.values()) for c in future_checks) if future_checks else None,
            'frozen_reference_parity_passed': all(bool(c) and all(c.values()) for c in reference_checks) if reference_checks else None,
        },
        'cpu': {
            'jobs_with_resource_snapshots': len(resources),
            'worker_elapsed_job_hours_excluding_startup': measured_worker_seconds / 3600 if measured_worker_seconds is not None else None,
            'worker_allocated_core_hours_excluding_startup': sum(r['elapsed_seconds'] * r.get('allocated_cpu', 4) for r in resources_list) / 3600 if resources else None,
            'observed_process_cpu_seconds': observed_cpu_seconds,
            'events_including_retries': len(cpu),
            'read_seconds_recorded_events': finite_sum(cpu, 'read_seconds'),
            'assembly_seconds_recorded_events': finite_sum(cpu, 'assemble_seconds'),
            'write_seconds_recorded_events': finite_sum(cpu, 'write_seconds'),
            'extra_reference_parity_seconds': finite_sum(cpu, 'parity_seconds'),
            'peak_measured_job_rss_bytes': maximum(resources_list, 'max_aggregate_rss_bytes'),
            'failed_job_snapshots': [r['job'] for r in resources_list if r.get('resource_error')],
        },
        'gpu': {
            'recorded_production_batches': len(gpu),
            'recorded_real_member_predictions_including_retries': sum(len(e.get('members', [])) for e in gpu),
            'gpu_inference_seconds': finite_sum(gpu, 'gpu_seconds'),
            'host_to_device_seconds': finite_sum(gpu, 'h2d_seconds'),
            'device_to_host_seconds': finite_sum(gpu, 'd2h_seconds'),
            'peak_allocated_bytes': maximum(gpu, 'peak_allocated_bytes'),
            'peak_reserved_bytes': maximum(gpu, 'peak_reserved_bytes'),
            'batch_numeric_gate': read(root / 'reports/mixed_batch_A100.json'),
            'timing_scope': 'One heartbeat per production batch; excludes model startup and batch-validation runs. GPU output-write time is not separately instrumented.',
        },
        'campaign_wall_seconds_including_queue_and_validation': wall,
        'cache': {
            'budget_bytes': read(root / 'config/deployment.json', {}).get('scratch_budget_bytes'),
            'peak_measured_bytes_lower_bound': queue.get('scratch_peak_measured_bytes'),
            'measurement_interval_seconds': 60,
            'reserved_bytes_current': sum(t.get('reserved', 0) for t in queue.get('tasks', {}).values()),
        },
        'slurm_accounting': dict(account, totals_by_kind=summarize_accounting(account)),
        'full_decade_comparison': {
            'progress': baseline_progress(operations.get('full_roots', []), tokens),
            'empirical_speedup': None,
            'reason': 'No direct speedup is inferred from selected-day fraction or unfinished full-decade jobs. Baseline resource histories and concurrency differ; compare audited completed costs separately.',
        },
        'validation_reports': {name: read(root / 'reports' / name) for name in [
            'pilot_passed.json', 'inputs_verified.json', 'products_verified.json', 'stage_error.json']},
        'incomplete_final_log_lines': incomplete,
        'interpretation': 'Retrospective observation-conditioned event experiment. Unselected dates are absent, not predicted zero. Cannot measure annual false-positive rates, annual emissions or operational forecast skill.',
    }
    return raw


def display(value, divisor=1, suffix=''):
    return 'Pending / not measured' if value is None else f'{value / divisor:,.3f}{suffix}'


def markdown(report):
    cpu, gpu, cache = (report[k] for k in ('cpu', 'gpu', 'cache'))
    lines = ['# WISP fire-episode experiment: resource and validation report', '',
             'Status: **' + report['phase'] + '**. Generated: ' + report['generated'] + '.', '',
             'This report separates selected-date reduction from measured computing cost. Missing measurements remain pending.', '',
             '| Region | Observed fire days | Selected days | Episodes | Short tasks |',
             '|---|---:|---:|---:|---:|']
    for token, summary in report['date_selection']['regions'].items():
        lines.append(f"| {token} | {summary['fire_days']} | {summary['selected_days']} | {summary['episodes']} | {summary['tasks']} |")
    selection = report['date_selection']
    lines += ['', f"Selected {selection['days']} of {selection['full_comparator_days']} region-days ({100 * selection['selected_day_fraction']:.2f}%). This percentage is **not** an observed speedup.", '',
              '| Measurement | Observed value |', '|---|---:|',
              '| Campaign elapsed time, including queue and verification | ' + display(report['campaign_wall_seconds_including_queue_and_validation'], 3600, ' h') + ' |',
              '| CPU worker job-hours, excluding initialization | ' + display(cpu['worker_elapsed_job_hours_excluding_startup'], suffix=' h') + ' |',
              '| Allocated worker CPU core-hours, excluding initialization | ' + display(cpu['worker_allocated_core_hours_excluding_startup'], suffix=' core h') + ' |',
              '| Observed process CPU time | ' + display(cpu['observed_process_cpu_seconds'], 3600, ' core h') + ' |',
              '| Input read / assemble / write, recorded worker events | ' + ' / '.join(display(cpu[k], suffix=' s') for k in ['read_seconds_recorded_events', 'assembly_seconds_recorded_events', 'write_seconds_recorded_events']) + ' |',
              '| Extra independent reference checks | ' + display(cpu['extra_reference_parity_seconds'], suffix=' s') + ' |',
              '| Largest measured CPU-job RSS | ' + display(cpu['peak_measured_job_rss_bytes'], 2**30, ' GiB') + ' |',
              '| Measured input-cache peak, lower bound | ' + display(cache['peak_measured_bytes_lower_bound'], 2**30, ' GiB') + ' |',
              '| Production GPU inference time | ' + display(gpu['gpu_inference_seconds'], suffix=' s') + ' |',
              '| Peak GPU allocated / reserved memory | ' + display(gpu['peak_allocated_bytes'], 2**30, ' GiB') + ' / ' + display(gpu['peak_reserved_bytes'], 2**30, ' GiB') + ' |', '',
              f"Input receipts cover {report['inputs']['completed_days_with_final_chunk_receipts']} days; {report['inputs']['pending_days_without_final_chunk_receipts']} days remain without a completed-chunk receipt. Partially built chunks are not called complete.", '',
              'CPU resource snapshots are one record per Slurm job, so repeated monitoring does not multiply job cost. Worker-event totals retain retries; GPU timings are counted once per batch, not once per date in the batch. GPU timings exclude model startup and the batch-equivalence gate.', '',
              'Slurm accounting: **' + report['slurm_accounting']['status'] + '**. Full-allocation job-hours and core-hours, when available, are stored by stage in the JSON report.', '',
              'The full-decade baseline remains a separately monitored workflow. This report does not invent a speedup from incomplete baselines or from different parallel resource allocations.', '',
              report['interpretation'], '']
    return '\n'.join(lines)


def write_report(c, query_sacct=True):
    """Controller API: accepts the event configuration or an explicit root path."""
    root = Path(c['root'] if isinstance(c, dict) else c)
    result = build_report(root, query_sacct)
    atomic(root / 'reports/resource_report.json', json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n')
    atomic(root / 'reports/RESOURCE_REPORT_EN.md', markdown(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--accounting', action='store_true')
    arguments = parser.parse_args()
    result = write_report(arguments.root, arguments.accounting)
    print(json.dumps({'phase': result['phase'], 'report': str(arguments.root / 'reports/resource_report.json')}))
