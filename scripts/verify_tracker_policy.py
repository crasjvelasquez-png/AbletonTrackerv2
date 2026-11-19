#!/usr/bin/env python3
"""Repeatable tracker-to-SQLite verification; never opens the user's database."""
import argparse
import contextlib
import dataclasses
import io
import json
import hashlib
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tracker

REPLAY_SECONDS=660
# The last playback sample stays valid for EVIDENCE_TTL; shutdown settles it.
CREDITED_SECONDS=REPLAY_SECONDS+int(tracker.EVIDENCE_TTL)


def replay_fixtures(out):
    fixtures=Path(__file__).resolve().parents[1]/'tests/fixtures/tracker'
    reports=[]
    for fixture in sorted(fixtures.glob('*.json')):
        case=json.loads(fixture.read_text())
        db=out/(fixture.stem+'.sqlite')
        with patch.object(tracker,'DB_PATH',db),contextlib.redirect_stdout(io.StringIO()):
            t=tracker.Tracker(writer_lock=False)
            for frame in case['ticks']:
                sec=frame['at']
                o=tracker.Observation(wall=1800000000+sec,mono=1000+sec,pid=42,
                    foreground=True,project=frame.get('project','Expiry fixture'),
                    project_at=1000+frame.get('project_at',sec),input_at=1000,
                    audio=frame.get('audio',False),audio_at=1000+frame.get('audio_at',sec),
                    source='fixture')
                t.poll_once(observation=o)
            t.shutdown()
        with contextlib.closing(sqlite3.connect(db)) as c:
            actual=c.execute('SELECT COALESCE(SUM(active_seconds),0) FROM sessions').fetchone()[0]
        reports.append(dict(name=case['name'],expected=case['expected_seconds'],actual=actual))
        assert actual==case['expected_seconds'],reports[-1]
    (out/'expiry-fixtures.json').write_text(json.dumps(reports,indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    out=args.output or Path(tempfile.mkdtemp(prefix='tracker-policy-'))
    out.mkdir(parents=True,exist_ok=True)
    db=out/'sessions.sqlite'
    if db.exists(): parser.error('Use a fresh output directory; refusing to overwrite evidence.')
    observations=[]; decisions=[]
    with patch.object(tracker,'DB_PATH',db),contextlib.redirect_stdout(io.StringIO()):
        t=tracker.Tracker()
        try:
            # Policy v3: uninterrupted playback is credited in full, with no listening cap.
            for sec in range(0,REPLAY_SECONDS+1,5):
                o=tracker.Observation(wall=1800000000+sec,mono=1000+sec,pid=42,
                    foreground=True,project='Verification set',project_at=1000+sec,
                    input_at=1000,observable=True,locked=False,audio=True,
                    audio_at=1000+sec,source='replay')
                observations.append(dataclasses.asdict(o))
                t.poll_once(observation=o)
                decisions.append(dataclasses.asdict(t.status()))
        finally: t.shutdown()
    with contextlib.closing(sqlite3.connect(db)) as c:
        actual={'active_seconds':c.execute('SELECT COALESCE(SUM(active_seconds),0) FROM sessions').fetchone()[0],
                'evidence_seconds':c.execute('SELECT COALESCE(SUM(credited_seconds),0) FROM activity_intervals').fetchone()[0],
                'open_sessions':c.execute('SELECT COUNT(*) FROM sessions WHERE end_time IS NULL').fetchone()[0]}
    expected={'active_seconds':CREDITED_SECONDS,'evidence_seconds':CREDITED_SECONDS,'open_sessions':0}
    lock=tracker.acquire_writer_lock(db)
    try:
        child=subprocess.run([sys.executable,'-c',
            'import sys; from pathlib import Path; import tracker; tracker.DB_PATH=Path(sys.argv[1]); tracker.Tracker()',str(db)],
            capture_output=True,text=True)
        assert child.returncode!=0 and 'Another tracker already owns' in child.stderr
        with contextlib.closing(sqlite3.connect(db)) as c:
            assert c.execute('SELECT SUM(active_seconds) FROM sessions').fetchone()[0]==CREDITED_SECONDS
        (out/'writer-contention.json').write_text(json.dumps({'second_writer_rejected':True,'totals_unchanged':True},indent=2)+'\n')
    finally:
        if lock is not None: lock.close()
    replay_fixtures(out)
    suite=unittest.defaultTestLoader.loadTestsFromName('test_tracker_policy')
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    revision=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()
    root=Path(__file__).resolve().parents[1]
    hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
        ['tracker.py','tracking_policy.py','activity_store.py','activity_observer.py','dashboard.py','menubar.py']}
    manifest={'source_sha256':hashes,'revision':revision,'python':platform.python_version(),'platform':platform.platform(),
              'policy_version':tracker.POLICY_VERSION,'type':'simulated adapters, real tracker and SQLite',
              'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}
    for name,data in [('manifest',manifest),('expected',expected),('actual',actual)]:
        (out/f'{name}.json').write_text(json.dumps(data,indent=2)+'\n')
    for name,data in [('observations',observations),('decisions',decisions)]:
        (out/f'{name}.jsonl').write_text(''.join(json.dumps(x,allow_nan=False)+'\n' for x in data))
    print(f'Verification artifacts: {out}')
    return 0 if result.wasSuccessful() and actual==expected else 1

if __name__=='__main__': raise SystemExit(main())
