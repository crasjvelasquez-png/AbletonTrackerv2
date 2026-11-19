#!/usr/bin/env python3
"""Tracker -> SQLite -> real HTTP acceptance checks with durable artifacts."""
import argparse
import contextlib
import dataclasses
from datetime import datetime
from http.server import HTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from urllib.request import urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tracker,dashboard


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path)
    p.add_argument('--serve',type=int,help='Keep the verified fixture dashboard available for browser inspection')
    a=p.parse_args()
    out=a.output or Path(tempfile.mkdtemp(prefix='tracker-http-'))
    out.mkdir(parents=True,exist_ok=True)
    db=out/'sessions.sqlite'
    if db.exists(): p.error('Use a fresh output directory')
    tracker.DB_PATH=dashboard.DB_PATH=db
    t=tracker.Tracker(writer_lock=False)
    with contextlib.closing(sqlite3.connect(db)) as c:
        dashboard.run_schema_migrations(c)
        c.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('session_condense_gap_minutes','0',0)")
        c.commit()
    base=datetime(2026,10,27,12).timestamp()
    trace=[]
    for start in (0,90):
        for sec in range(start,start+36,5):
            o=tracker.Observation(wall=base+sec,mono=1000+sec,pid=42,foreground=True,
                project='HTTP verification set',project_at=1000+sec,input_at=1000+start,
                audio=False,audio_at=1000+sec,source='fixture')
            t.poll_once(observation=o)
            trace.append(dataclasses.asdict(t.status()))
    t.shutdown()
    server=HTTPServer(('127.0.0.1',a.serve or 0),dashboard.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    def fetch():
        with urlopen(url+'/api/data') as r: return json.load(r)
    try:
        first=fetch()
        assert first['summary']['total_seconds']==60,first['summary']
        assert first['summary']['today_seconds']==60,first['summary']
        assert first['summary']['tracking_active'] is False
        assert first['summary']['tracking_status']['reason']=='idle'
        assert first['summary']['today_session_count']==2
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.row_factory=sqlite3.Row
            assert dashboard.get_daily_progress_seconds(c,datetime.fromtimestamp(base).date())==60
            before=c.execute('SELECT SUM(credited_seconds) FROM activity_intervals').fetchone()[0]
        merge=dashboard.consolidate_sessions()
        assert merge['merged']==1,merge
        second=fetch()
        assert second['summary']['total_seconds']==60
        with contextlib.closing(sqlite3.connect(db)) as c:
            evidence=c.execute('SELECT SUM(credited_seconds) FROM activity_intervals').fetchone()[0]
            assert before==evidence==60
            assert c.execute('SELECT COUNT(DISTINCT session_id) FROM activity_intervals').fetchone()[0]==1
            assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        # Legacy and policy-covered rows must not be physically combined.
        with contextlib.closing(sqlite3.connect(db)) as c:
            c.execute('INSERT INTO sessions(project_name,start_time,last_seen_time,end_time,active_seconds,notes) VALUES(?,?,?,?,?,?)',
                      ('HTTP verification set',base+160,base+170,base+170,10,'Keep this historical note'))
            c.commit()
        assert dashboard.consolidate_sessions()['merged']==0
        results={'http_total_before_and_after_merge':60,'evidence_seconds':60,
                 'short_pause_evidence':'preserved','mixed_precision_merge':'blocked',
                 'integrity':'ok','url':url,'type':'real tracker, SQLite, HTTP; simulated macOS observations'}
        (out/'http-before.json').write_text(json.dumps(first,indent=2)+'\n')
        (out/'http-after.json').write_text(json.dumps(second,indent=2)+'\n')
        (out/'trace.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in trace))
        (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
        print(f'HTTP verification passed. Artifacts: {out}',flush=True)
        if a.serve:
            print(f'Fixture dashboard: {url}',flush=True)
            while True: time.sleep(1)
    finally:
        server.shutdown();server.server_close()

if __name__=='__main__': main()
