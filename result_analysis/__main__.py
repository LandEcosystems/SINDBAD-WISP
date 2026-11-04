import argparse,json,os,sys,time,traceback
from pathlib import Path
from .io import atomic_json,file_hash

def main():
    p=argparse.ArgumentParser(description='Read-only SINDBAD WISP results analysis')
    p.add_argument('--config',default=str(Path(__file__).with_name('config.json')))
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('inventory');sub.add_parser('audit')
    a=sub.add_parser('region');a.add_argument('--region');a.add_argument('--index',type=int)
    a=sub.add_parser('references');a.add_argument('--region')
    sub.add_parser('report');sub.add_parser('status')
    args=p.parse_args();config=json.loads(Path(args.config).read_text())
    for key,value in list(config.items()):
        if isinstance(value,str):
            config[key]=os.path.expandvars(value)
            if '${' in config[key]:raise ValueError('Set required environment variable for '+key+': '+value)
    for key,env in [('source_geometry','WISP_ANALYSIS_GEOMETRY'),('pyrome_path','WISP_ANALYSIS_PYROME'),
                    ('mod16_archive_root','WISP_ANALYSIS_MOD16_ARCHIVE')]:
        if key in config:os.environ[env]=config[key]
    root=Path(config['output_root'])
    root.mkdir(parents=True,exist_ok=True)
    if args.command=='inventory':
        from .registry import inventory,components,VARIABLES,UNAVAILABLE
        snap=inventory(config['source_root'],root/'inventory.json')
        (root/'region_ids.txt').write_text('\n'.join(r['region_id'] for r in snap['regions'])+'\n')
        # Stable country/longitude ordering, no dependence on model effect or reference agreement.
        records=snap['regions'];ids=[r['region_id'] for r in records]
        chosen=[ids[0],ids[len(ids)//2],ids[-1]]
        atomic_json(root/'pilot.json',dict(region_ids=chosen,selection='First, middle, last lexicographically sorted completed region IDs; fixed before reading numerical results.'))
        (root/'pilot_ids.txt').write_text('\n'.join(chosen)+'\n')
        atomic_json(root/'variable_registry.json',dict(variables=VARIABLES,components=components(),unavailable=UNAVAILABLE))
        atomic_json(root/'config.json',config)
        print(json.dumps(dict(regions=len(ids),pilot=chosen)))
    elif args.command=='audit':
        from .audit import audit_inventory
        print(json.dumps(audit_inventory(config)))
    elif args.command=='region':
        from .engine import process_region
        from .audit import validate_dependencies
        snap=json.loads((root/'inventory.json').read_text())
        rid=args.region or snap['regions'][args.index]['region_id']
        try:
            from .integrity import verify_region_artifacts
            if (root/'regions'/rid/'completion.json').exists():
                verify_region_artifacts(root/'regions'/rid)
            provenance=validate_dependencies(config,rid)
            result=process_region(config,rid,snap['source_metadata_fingerprint'])
            verify_region_artifacts(root/'regions'/rid)
            from .context import build_context
            build_context(config,rid)
            validate_dependencies(config,rid)
            atomic_json(root/'regions'/rid/'provenance.json',provenance)
            print(json.dumps(result))
        except Exception as e:
            atomic_json(root/'regions'/rid/'error.json',dict(type=type(e).__name__,message=str(e),traceback=traceback.format_exc(),time=time.time()))
            raise
    elif args.command=='references':
        from .references import compare_region
        from .integrity import verify_region_artifacts
        regions=[root/'regions'/args.region] if args.region else [p.parent for p in (root/'regions').glob('*/completion.json')]
        for d in regions:
            verify_region_artifacts(d)
            compare_region(d/'compact.nc',d/'references',config)
    elif args.command=='report':
        from .report import generate
        from .integrity import verify_region_artifacts
        verified=[verify_region_artifacts(p.parent) for p in (root/'regions').glob('*/completion.json')]
        atomic_json(root/'artifact_integrity.json',dict(status='pass',regions=verified))
        print(generate(root))
    else:
        done=[json.loads(p.read_text()) for p in (root/'regions').glob('*/completion.json')]
        errs=[str(p.parent.name) for p in (root/'regions').glob('*/error.json') if not (p.parent/'completion.json').exists()]
        print(json.dumps(dict(processed=len(done),pass_count=sum(d['scientific_status']=='pass' for d in done),fail_count=sum(d['scientific_status']=='fail' for d in done),errors=errs),indent=2))

if __name__=='__main__':main()
