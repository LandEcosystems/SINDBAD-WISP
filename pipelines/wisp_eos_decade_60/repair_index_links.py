"""Replace lane-B index symlinks with hard links without changing lock inodes.

This removes an extra network-path resolution step. It does not establish that
symlinks caused the observed transient ENXIO. Never unlink/recreate the source
lock, and never reset tasks with a live owner. Existing error logs are retained.
"""
import argparse
import copy
import os
from pathlib import Path
from campaign import atomic, engine, read, utc


def replace_link(source, destination):
    source, destination = Path(source), Path(destination)
    before = source.stat()
    if destination.is_symlink():
        if destination.resolve() != source.resolve():
            raise ValueError('Unexpected symlink target')
        temp = destination.with_name(destination.name + '.hardlink.' + str(os.getpid()))
        os.link(str(source), str(temp))
        try:
            st = temp.stat()
            if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('Different lock inode')
            os.replace(str(temp), str(destination))
        finally:
            if temp.exists():
                temp.unlink()
    after = destination.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError('Existing destination is a different lock')
    return dict(path=str(destination), source=str(source), device=after.st_dev, inode=after.st_ino)


def main():
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path)
    p.add_argument('--requeue', action='store_true'); args = p.parse_args()
    root = args.root.resolve(); b = root / 'lanes/lane_b'
    paths = list((root / 'locks').glob('index_*.lock'))
    if not paths:
        raise ValueError('No index locks')
    records = [replace_link(f, b / 'locks' / f.name) for f in paths]
    out = root / 'campaigns/dual120_v1'
    atomic(out / 'index_hardlinks.json', dict(at=utc(), locks=records,
           original_lock_inodes_preserved=True, cause_of_transient_enxio_not_proven=True))
    restored = {}
    if args.requeue:
        rt, ctl, migration, c = engine(b)
        with rt.Queue(c).edit() as q:
            for key, t in q['tasks'].items():
                if t.get('input_status') != 'blocked':
                    continue
                error = t.get('error', '')
                if not ('[Errno 6]' in error and '/locks/index_' in error):
                    continue
                if t.get('lease') or t.get('job') or t.get('pid'):
                    raise ValueError('Failed task still owned')
                restored[key] = copy.deepcopy(t)
                t.update(input_status='queued', status='queued', retry_after=0,
                         repaired_index_link_at=utc(), previous_error=error)
                t.pop('error', None)
            atomic(out / ('index_requeue_' + str(os.getpid()) + '.json'), dict(at=utc(), before=restored))
    print('LOCKS', len(records), 'REQUEUED', sorted(restored), flush=True)


if __name__ == '__main__':
    main()
