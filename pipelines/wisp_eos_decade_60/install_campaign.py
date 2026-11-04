"""Install a checked campaign beside an existing frozen engine; never edit it."""
import argparse
from pathlib import Path
import shlex
import shutil
import campaign as c


def copy_exact(source, dest):
    if dest.exists():
        if c.sha(source) != c.sha(dest):
            raise ValueError('Refusing to replace an installed campaign: ' + str(dest))
        return
    shutil.copy2(source, dest)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--run-root', type=Path, default=c.DEFAULT_ROOT)
    p.add_argument('--git-commit', required=True)
    args = p.parse_args(); root=args.run_root.resolve(); source=args.manifest.resolve()
    m=c.load_manifest(root,source)
    # Confirm the archived engine is exactly the deployed executable release.
    bundled=Path(__file__).parent/'engine'
    if c.sha(bundled/'release.json') != m['engine_release_sha256']:
        raise ValueError('Repository engine differs from selected deployment')
    for name, expected in c.read(bundled/'release.json')['files'].items():
        if c.sha(bundled/name) != expected or c.sha(root/'code'/name) != expected:
            raise ValueError('Engine file identity mismatch: '+name)
    dest=root/'campaigns'/m['campaign_id']; dest.mkdir(parents=True,exist_ok=True)
    for f in [source,source.with_suffix('.sha256'),source.with_suffix('.csv'),Path(__file__).parent/'campaign.py']:
        copy_exact(f,dest/f.name)
    script=(root/'code/job.sh').read_text(); lines=script.splitlines()
    if not lines[-1].startswith('exec ') or 'controller.py' not in lines[-1]:
        raise ValueError('Unknown engine launcher; refuse to guess')
    command=['exec',c.PYTHON,'-u',str(dest/'campaign.py'),'control','--run-root',str(root),'--manifest',str(dest/'manifest.json')]
    lines[-1]=' '.join(shlex.quote(a) for a in command)
    content='\n'.join(lines)+'\n'; launcher=dest/'run_control.sh'
    if launcher.exists() and launcher.read_text()!=content:
        raise ValueError('Existing launcher differs')
    launcher.write_text(content); launcher.chmod(0o755)
    identity={'git_commit':args.git_commit,'campaign_manifest_sha256':c.sha(dest/'manifest.json'),
              'files':{name:c.sha(dest/name) for name in ['campaign.py','run_control.sh']}}
    ip=dest/'campaign_code.json'
    if ip.exists() and c.read(ip)!=identity:raise ValueError('Installed code identity differs')
    c.atomic(ip,identity)
    print(dest)
    print('Installed only: no controller stopped, no jobs submitted, no queue reset.')


if __name__=='__main__':main()
