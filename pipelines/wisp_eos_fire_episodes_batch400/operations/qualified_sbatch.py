#!/usr/bin/python3
"""Campaign-only Slurm wrapper: CPU work uses previously qualified hosts.

Deploy this file as bin/sbatch with node_policy.json in its parent directory.
GPU inference and all unrelated jobs pass through unchanged. The numerical
probe in each producer remains enabled; this is only a scheduling restriction.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def job_name(args):
    for i, arg in enumerate(args):
        if arg.startswith('--job-name='):
            return arg.split('=', 1)[1]
        if arg in ('--job-name', '-J') and i + 1 < len(args):
            return args[i + 1]
    return ''


def restricted_args(args, policy, cluster_nodes):
    name = job_name(args)
    if not name.startswith('w4e-') or name == 'w4e-gpu':
        return list(args)
    allowed = set(policy['qualified_hosts'])
    current = set(cluster_nodes)
    if not allowed or not current or not allowed.intersection(current):
        raise ValueError('No qualified CPU host in current Slurm inventory')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', host) for host in allowed | current):
        raise ValueError('Expected expanded Slurm node names')
    excluded = sorted(current - allowed)
    remaining = []
    extra = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith('--exclude='):
            extra.append(arg.split('=', 1)[1])
        elif arg in ('--exclude', '-x'):
            i += 1
            if i == len(args):
                raise ValueError('Missing exclude argument')
            extra.append(args[i])
        else:
            remaining.append(arg)
        i += 1
    expression = ','.join(excluded + extra)
    return (['--exclude=' + expression] if expression else []) + remaining


def main():
    args = sys.argv[1:]
    name = job_name(args)
    if name.startswith('w4e-') and name != 'w4e-gpu':
        policy_path = Path(__file__).resolve().parent.parent / 'node_policy.json'
        policy = json.loads(policy_path.read_text())
        result = subprocess.run(['/usr/bin/sinfo', '-N', '-h', '-o', '%N'],
                                check=True, universal_newlines=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30)
        args = restricted_args(args, policy, result.stdout.split())
    os.execv('/usr/bin/sbatch', ['/usr/bin/sbatch'] + args)


if __name__ == '__main__':
    main()
