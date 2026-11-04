"""Read-only campaign progress: a claimed month is not a completed month."""
import argparse
from collections import Counter
import datetime
import json
from pathlib import Path
import time
from campaign import DEFAULT_ROOT, read

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-root',type=Path,default=DEFAULT_ROOT);a=p.parse_args()
r=a.run_root;q=read(r/'state/queue.json');c=read(r/'config/frozen_deployment.json');token=q['active_cube']
tasks={k:v for k,v in q['tasks'].items() if v['region']==token and v['kind']=='month'}
files=[f for key in tasks for f in (Path(c['scratch'])/'inputs'/key/'receipts').glob('*.json')]
now=time.time();times=[f.stat().st_mtime for f in files];progress=read(r/'reports/progress.json')
print(json.dumps({'checked_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'controller_report_utc':progress['at'],'active_cube':token,'phase':q['phase'],
 'campaign':q.get('campaign_id'),'completed_cubes':q.get('completed_cubes',[]),
 'input_month_states':dict(Counter(t['input_status'] for t in tasks.values())),
 'retained_input_day_receipts':len(files),'expected_days':3641,
 'new_receipts_last_hour':sum(t>=now-3600 for t in times),
 'jobs_in_last_controller_report':dict(Counter(j['kind']+':'+j['state'] for j in progress['jobs'])),
 'pilot_passed':progress['pilot_passed'],
 'note':'After cleanup, zero retained input receipts is expected. Use final product validation, not receipt count, to judge completed cubes.'},indent=2))
