#!/usr/bin/env python3
"""Prepare a stopped tracker DB for an older runtime, preserving all totals.

Run only when rolling back source/bundle. Makes a mandatory SQLite backup first.
New-policy intervals are retained for review, but their sessions become legacy
estimates so an older runtime can safely update the aggregates. No DB is deleted.
"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tracker import acquire_writer_lock


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    p.add_argument('--backup',type=Path,required=True)
    a=p.parse_args()
    if not a.db.is_file() or a.backup.exists(): p.error('DB must exist and backup must be a new path')
    lock=acquire_writer_lock(a.db)
    if lock is None: p.error('Stop the tracker before preparing rollback')
    try:
        a.backup.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(a.db)) as c,closing(sqlite3.connect(a.backup)) as b:
            c.backup(b)
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE sessions SET end_time=COALESCE(last_seen_time,start_time) WHERE end_time IS NULL')
            c.execute('UPDATE sessions SET policy_version=0 WHERE policy_version>0')
            c.execute('DELETE FROM tracker_status')
            c.commit()
            assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        print(f'Rollback prepared; all session totals retained. Backup: {a.backup}')
    finally: lock.close()

if __name__=='__main__': main()
