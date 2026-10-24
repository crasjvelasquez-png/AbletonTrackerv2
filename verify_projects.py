#!/usr/bin/env python3
"""Repeatable project-launcher integration check; never uses the user's database.

Failure coverage: corrupt settings, missing/duplicate folders, unsafe labels,
recent ordering/deduplication/limit, backups, duplicate set names, unavailable
Spotlight, removed files, failed launch, HTTP validation and persistence.
Writes a JSON evidence artifact to --output (default /tmp/tracker-projects.json).
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import sys
from types import SimpleNamespace
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import dashboard
import projects
import tracker


def verify(output):
    checks = []
    with tempfile.TemporaryDirectory(prefix="tracker-projects-") as tmp:
        root = Path(tmp).resolve()
        db = root / "sessions.db"
        tracker.DB_PATH = dashboard.DB_PATH = db
        tracker.setup_db()
        with sqlite3.connect(db) as conn:
            dashboard.run_schema_migrations(conn)
            for i in range(12):
                conn.execute("INSERT INTO sessions(project_name,start_time,last_seen_time,end_time,active_seconds) VALUES(?,?,?,?,?)", (f"Song {i}", i, i + 10, i + 10, 10))
            conn.execute("INSERT INTO sessions(project_name,start_time,last_seen_time,end_time,active_seconds) VALUES('Song 4',100,200,200,100)")
            conn.commit()
        folder = root / "Sets with spaces"
        (folder / "Backup").mkdir(parents=True)
        (folder / "Album").mkdir()
        first = folder / "Song 4.als"
        newer = folder / "Album" / "Song 4.als"
        for p in (first, newer, folder / "Backup" / "Song 4.als"):
            p.touch()
        os.utime(first, (10, 10))
        os.utime(newer, (20, 20))
        server = HTTPServer(("127.0.0.1", 0), dashboard.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def save(value):
            req = Request(base + "/api/app-settings", data=json.dumps({"key": projects.PINNED_FOLDERS_KEY, "value": value}).encode(), headers={"Content-Type": "application/json"})
            with urlopen(req) as response:
                return json.load(response)

        try:
            for bad in ('broken json', '{}', '[{"path":"relative"}]', json.dumps([{"path": str(first)}]), json.dumps([{"path": str(folder), "label": "bad\nlabel"}])):
                try:
                    save(bad)
                    raise AssertionError(f"accepted invalid folders: {bad}")
                except HTTPError as exc:
                    assert exc.code == 400
            pins = [{"path": str(folder), "label": "Work"}]
            save(json.dumps(pins))
            try:
                save(json.dumps(pins * 2))
                raise AssertionError("accepted duplicate folder")
            except HTTPError as exc:
                assert exc.code == 400
            assert projects.load_pinned_folders(db) == pins
            checks.append("HTTP validation and persisted folder configuration")
            with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError):
                snapshot = projects.build_catalog(db)
            assert len(snapshot["recent"]) == 10
            assert snapshot["recent"][0]["name"] == "Song 4"
            assert snapshot["recent"][0]["path"] == str(newer)
            assert len({r["name"] for r in snapshot["recent"]}) == 10
            assert len(snapshot["folders"][0]["projects"]) == 2
            assert all("Backup" not in p["path"] for p in snapshot["folders"][0]["projects"])
            checks.append("10 distinct ordered recents, newest duplicate match, Backup exclusion")
            if sys.platform == 'darwin':
                import rumps
                import menubar
                from unittest.mock import Mock
                menu = rumps.MenuItem('Open Project')
                menu.add('Loading projects…')
                app = SimpleNamespace(
                    _next_project_refresh=float('inf'),
                    project_catalog=SimpleNamespace(take_snapshot=lambda: snapshot),
                    _project_snapshot=None, open_project_item=menu,
                    _open_project=Mock(), customize_projects=Mock(),
                )
                menubar.AbletonTrackerApp._refresh_projects(app)
                assert menu['recent-0'].title == 'Song 4'
                menu['recent-0'].callback(menu['recent-0'])
                app._open_project.assert_called_once_with(str(newer))
                assert menu['recent-1'].callback is None
                assert len(menu['folder-0']) == 2
                menu['Customize…'].callback(menu['Customize…'])
                app.customize_projects.assert_called_once()
                checks.append("native rumps menu structure, disabled unresolved set, correct click dispatch, Customize callback")
            with patch.object(projects.subprocess, "run") as launch:
                launch.return_value.returncode = 0
                projects.open_project(str(newer), "/Applications/Live.app")
                assert launch.call_args.args[0] == ["/usr/bin/open", "-a", "/Applications/Live.app", str(newer)]
                launch.return_value.returncode = 1
                launch.return_value.stderr = "launch failed"
                try:
                    projects.open_project(str(newer), "/Applications/Live.app")
                    raise AssertionError("failed launch was ignored")
                except RuntimeError:
                    pass
            newer.unlink()
            try:
                projects.open_project(str(newer), None)
                raise AssertionError("removed project accepted")
            except ValueError:
                pass
            checks.append("safe launch argument boundaries and missing-file/launch errors")
            renamed = [{"path": str(folder), "label": "Renamed"}, {"path": str(root / "offline"), "label": "Offline"}]
            save(json.dumps(renamed[::-1]))
            with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError):
                updated = projects.build_catalog(db)
            assert updated["folders"][0]["missing"]
            assert updated["folders"][1]["label"] == "Renamed"
            save('[]')
            assert projects.load_pinned_folders(db) == []
            checks.append("rename/reorder/remove persistence and missing folders")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    artifact = {"ok": True, "checks": checks, "catalog": snapshot, "launch": "command verified with a recording substitute; no real set opened"}
    Path(output).write_text(json.dumps(artifact, indent=2))
    print(json.dumps({"ok": True, "checks": len(checks), "artifact": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='/tmp/tracker-projects.json')
    verify(parser.parse_args().output)
