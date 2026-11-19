#!/usr/bin/env python3
"""Measure an existing tracker without changing Live or the user's database."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time


def seconds(value):
    parts=value.replace('-',':').split(':')
    total=0.0
    for p in parts: total=total*60+float(p)
    return total


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True)
    p.add_argument('--seconds',type=float,default=600)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    bundle=Path(__file__).resolve().parents[1]/'dist/Tracker.app/Contents/Resources/app'
    source_hashes={name:hashlib.sha256((bundle/name).read_bytes()).hexdigest()
                   for name in ['tracker.py','activity_observer.py','activity_store.py','tracking_policy.py','menubar.py','dashboard.py']
                   if (bundle/name).exists()}
    rows=[]; start=time.monotonic()
    while True:
        result=subprocess.run(['ps','-axo','pid=,ppid=,time=,%cpu=,rss=,comm='],capture_output=True,text=True,check=True)
        processes=[]
        for line in result.stdout.splitlines():
            fields=line.split(None,5)
            if len(fields)!=6: continue
            pid,ppid,cpu,percent,rss,command=fields
            if int(pid)==a.pid or int(ppid)==a.pid or command.rsplit('/',1)[-1] in ('replayd','screencapture','WindowServer'):
                processes.append(dict(pid=int(pid),ppid=int(ppid),cpu_seconds=seconds(cpu),cpu_percent=float(percent),rss_kib=int(rss),command=command))
        elapsed=time.monotonic()-start
        rows.append(dict(elapsed=elapsed,processes=processes))
        if elapsed>=a.seconds: break
        time.sleep(min(5,a.seconds-elapsed))
    first=next((x for x in rows[0]['processes'] if x['pid']==a.pid),None)
    last=next((x for x in rows[-1]['processes'] if x['pid']==a.pid),None)
    cpu=last['cpu_seconds']-first['cpu_seconds'] if first and last else None
    report=dict(bundle_source_sha256=source_hashes,pid=a.pid,duration_seconds=rows[-1]['elapsed'],parent_cpu_seconds=cpu,
                parent_average_percent_one_core=100*cpu/rows[-1]['elapsed'] if cpu is not None else None,
                limitations='Existing user workload, not controlled editing/playback; transient children may be missed; capture-service costs are shared.',samples=rows)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='samples'},indent=2))

if __name__=='__main__': main()
