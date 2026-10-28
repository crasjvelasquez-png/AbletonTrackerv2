#!/usr/bin/env python3
"""Seeded mixed-workload replay through the tracker and real SQLite.

Checks persistence and allocation invariants, including clock corrections and
transient write failures. Does not claim to qualify hardware or permissions.
"""
import argparse
from contextlib import closing,redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tracker


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seed',type=int,default=27)
    p.add_argument('--frames',type=int,default=2000)
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    out=a.output or Path(tempfile.mkdtemp(prefix='tracker-stress-'))
    out.mkdir(parents=True,exist_ok=True)
    db=out/'sessions.sqlite'
    if db.exists(): p.error('Use a fresh output directory')
    rng=random.Random(a.seed)
    input_at=None; focused=True; wall=1800000000.0
    observations=[]; failures=0
    with patch.object(tracker,'DB_PATH',db),redirect_stdout(io.StringIO()):
        t=tracker.Tracker(writer_lock=False)
        for frame in range(a.frames):
            mono=1000.0+frame*5
            if rng.random()<0.04:
                focused=not focused; input_at=None
            if focused and rng.random()<0.24: input_at=mono
            if rng.random()<0.01: wall+=rng.choice([-3600,3600])
            wall+=5
            o=tracker.Observation(wall=wall,mono=mono,pid=42,foreground=focused,
                project='Stress fixture',project_at=mono,input_at=input_at,
                observable=rng.random()>0.02,locked=rng.random()<0.01,
                audio=rng.choice([True,False,None]),audio_at=mono,source='seeded_fixture')
            observations.append(o.__dict__)
            try:
                if rng.random()<0.02:
                    with patch.object(tracker.sqlite3,'connect',side_effect=sqlite3.OperationalError('injected unavailable database')):
                        t.poll_once(observation=o)
                else: t.poll_once(observation=o)
            except sqlite3.OperationalError: failures+=1
        t.poll_once(observation=tracker.Observation(wall=wall+5,mono=mono+5,pid=42,foreground=False))
        t.shutdown()
        with closing(sqlite3.connect(db)) as c:
            c.row_factory=sqlite3.Row
            totals=c.execute('SELECT s.id,s.active_seconds,COALESCE(SUM(a.credited_seconds),0) evidence FROM sessions s LEFT JOIN activity_intervals a ON a.session_id=s.id GROUP BY s.id').fetchall()
            assert all(abs(r['active_seconds']-r['evidence'])<1e-6 for r in totals),'Aggregate/evidence disagreement'
            spans=c.execute('SELECT start_time,end_time,credited_seconds FROM activity_intervals WHERE credited_seconds>0').fetchall()
            assert all(r['credited_seconds']>=0 and r['end_time']>=r['start_time'] for r in spans),'Invalid span'
            total=sum(r['active_seconds'] for r in totals)
            assert total<=a.frames*5,'More credit than elapsed awake time'
            daily=sum(tracker._all_daily_totals().values())
            assert abs(total-daily)<1e-6,'Calendar allocation loses or invents time'
            assert c.execute('SELECT COUNT(*) FROM sessions WHERE end_time IS NULL').fetchone()[0]==0
            assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    result={'seed':a.seed,'frames':a.frames,'simulated_seconds':a.frames*5,'injected_write_failures':failures,
            'recorded_seconds':total,'intervals':len(spans),'aggregate_equals_evidence':True,
            'calendar_allocation_equals_total':True,'integrity':'ok'}
    (out/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    (out/'observations.jsonl').write_text(''.join(json.dumps(o)+'\n' for o in observations))
    print(json.dumps(result,indent=2)); print('Stress artifacts:',out)

if __name__=='__main__': main()
