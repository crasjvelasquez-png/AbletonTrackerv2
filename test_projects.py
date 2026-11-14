import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_app
import dashboard
import projects
import tracker


class ProjectNormalizationAndResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tracker-proj-test-")
        self.root = Path(self._tmp.name).resolve()
        self.db = self.root / "sessions.db"
        self._tracker_db = patch.object(tracker, "DB_PATH", self.db)
        self._dash_db = patch.object(dashboard, "DB_PATH", self.db)
        self._tracker_db.start()
        self._dash_db.start()
        tracker.setup_db()
        with sqlite3.connect(self.db) as conn:
            dashboard.run_schema_migrations(conn)

    def tearDown(self):
        self._dash_db.stop()
        self._tracker_db.stop()
        self._tmp.cleanup()

    def _pin_folder(self, folder: Path):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)",
                (projects.PINNED_FOLDERS_KEY, f'[{{"path": "{folder}", "label": "Sets"}}]', 1.0),
            )
            conn.commit()

    def _add_session(self, name: str, start: float, end: float):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO sessions(project_name, start_time, last_seen_time, end_time, active_seconds) "
                "VALUES(?, ?, ?, ?, ?)",
                (name, start, end, end, end - start),
            )
            conn.commit()

    def test_pinned_folder_resolves_double_space_trailing_space_parens_nfc_and_ignores_appledouble(self):
        folder = self.root / "ExternalDrive"
        folder.mkdir()
        (folder / "Backup").mkdir()

        double_space = folder / "October 2026 - Check it - 93 bpm  12.4.als"
        apple_double = folder / "._October 2026 - Check it - 93 bpm  12.4.als"
        backup_file = folder / "Backup" / "October 2026 - Check it - 93 bpm  12.4.als"
        trailing_space = folder / "First of the Month - Misa James - Mixing Session .als"
        parens_file = folder / "lonely in LA - clara zimm (+miloproduces).als"
        nfd_file = folder / "Cafe\u0301 Song.als"

        for p in (double_space, apple_double, backup_file, trailing_space, parens_file, nfd_file):
            p.touch()
        os.utime(apple_double, (999999, 999999))
        os.utime(backup_file, (999999, 999999))
        os.utime(double_space, (100, 100))

        self._pin_folder(folder)
        self._add_session("October 2026 - Check it - 93 bpm 12.4", 400, 500)
        self._add_session("First of the Month - Misa James - Mixing Session", 300, 400)
        self._add_session("lonely in LA - clara zimm", 200, 300)
        self._add_session("Caf\u00e9 Song", 100, 200)

        with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(
                projects.resolve_project_path(self.db, "October 2026 - Check it - 93 bpm 12.4"),
                str(double_space),
            )
            self.assertEqual(
                projects.resolve_project_path(self.db, "First of the Month - Misa James - Mixing Session"),
                str(trailing_space),
            )
            self.assertEqual(
                projects.resolve_project_path(self.db, "lonely in LA - clara zimm"),
                str(parens_file),
            )
            self.assertEqual(
                projects.resolve_project_path(self.db, "Caf\u00e9 Song"),
                str(nfd_file),
            )
            catalog = projects.build_catalog(self.db)

        recent = {r["name"]: r["path"] for r in catalog["recent"]}
        self.assertEqual(recent["October 2026 - Check it - 93 bpm 12.4"], str(double_space))
        self.assertEqual(recent["First of the Month - Misa James - Mixing Session"], str(trailing_space))
        self.assertEqual(recent["lonely in LA - clara zimm"], str(parens_file))
        self.assertEqual(recent["Caf\u00e9 Song"], str(nfd_file))
        listed_paths = {p["path"] for p in catalog["folders"][0]["projects"]}
        self.assertNotIn(str(apple_double), listed_paths)
        self.assertNotIn(str(backup_file), listed_paths)

    def test_exact_and_collapsed_matches_beat_newer_parenthetical_match(self):
        folder = self.root / "Sets"
        folder.mkdir()
        collapsed = folder / "My Track .als"
        paren = folder / "My Track (demo).als"
        collapsed.touch()
        paren.touch()
        os.utime(collapsed, (100, 100))
        os.utime(paren, (500, 500))
        self._pin_folder(folder)

        with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(projects.resolve_project_path(self.db, "My Track"), str(collapsed))

        exact = folder / "My Track.als"
        exact.touch()
        os.utime(exact, (50, 50))
        with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(projects.resolve_project_path(self.db, "My Track"), str(exact))

    def test_spotlight_wildcard_fallback_skips_appledouble_backup_and_nonmatching_candidates(self):
        drive = self.root / "T7"
        (drive / "Backup").mkdir(parents=True)
        real_match = drive / "October 2026 - Check it - 93 bpm  12.4.als"
        other_set = drive / "October 2026 - Check it - 93 bpm .als"
        apple_double = drive / "._October 2026 - Check it - 93 bpm  12.4.als"
        backup_set = drive / "Backup" / "October 2026 - Check it - 93 bpm  12.4.als"
        for p in (real_match, other_set, apple_double, backup_set):
            p.touch()
        os.utime(other_set, (900, 900))
        os.utime(apple_double, (950, 950))
        os.utime(backup_set, (960, 960))
        os.utime(real_match, (200, 200))

        queries = []

        def fake_mdfind(args, capture_output=True, timeout=5):
            query = args[2]
            queries.append(query)
            if len(queries) == 1:
                # Exact query misses because of double space.
                return subprocess.CompletedProcess(args, 0, b"", b"")
            # Wildcard query returns real_match plus AppleDouble, Backup, and non-matching 93 bpm set.
            payload = "\0".join([str(apple_double), str(backup_set), str(other_set), str(real_match)]).encode("utf-8")
            return subprocess.CompletedProcess(args, 0, payload, b"")

        with patch.object(projects.subprocess, "run", side_effect=fake_mdfind):
            resolved = projects.resolve_project_path(self.db, "October 2026 - Check it - 93 bpm 12.4")

        self.assertEqual(resolved, str(real_match))
        self.assertEqual(len(queries), 2)
        self.assertIn('kMDItemFSName == "*.als"c', queries[1])
        self.assertIn("*October*", queries[1])
        self.assertIn("*Check*", queries[1])
        self.assertNotIn("*October*Check*", queries[1])

    def test_resolve_song_path_falls_back_across_song_files_by_stage_priority_and_recency(self):
        folder = self.root / "FallbackSets"
        folder.mkdir()
        mix_old = folder / "Neon Nights - Mixing.als"
        song_file = folder / "Neon Nights.als"
        mix_old.touch()
        song_file.touch()
        self._pin_folder(folder)

        song = {
            "key": "neon nights",
            "title": "Neon Nights",
            "stage": "mastering",
            "open_name": "Neon Nights - Master 12.4",
            "files": [
                {"name": "Neon Nights - Master 12.4", "stage": "mastering", "last_seen": 500},
                {"name": "Neon Nights.als"[:-4], "stage": None, "last_seen": 400},
                {"name": "Neon Nights - Mixing", "stage": "mixing", "last_seen": 300},
            ],
        }
        with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError):
            # Missing mastering file should prefer mixing (higher stage priority) over song file.
            self.assertEqual(projects.resolve_song_path(self.db, song), str(mix_old))
            mix_old.unlink()
            self.assertEqual(projects.resolve_song_path(self.db, song), str(song_file))
            song_file.unlink()
            with self.assertRaises(ValueError) as ctx:
                projects.resolve_song_path(self.db, song)
            self.assertIn("Neon Nights - Master 12.4.als", str(ctx.exception))

    def test_dashboard_open_uses_song_fallback(self):
        folder = self.root / "DeckSets"
        folder.mkdir()
        fallback_set = folder / "Aurora.als"
        fallback_set.touch()
        self._pin_folder(folder)

        self._add_session("Aurora", 100, 200)
        self._add_session("Aurora - Mixing 12.4", 300, 400)
        import songs as song_model
        song_model.put_on_deck(self.db, "aurora")

        opened_dash = []
        with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError), \
             patch.object(dashboard, "open_project", side_effect=lambda p, app: opened_dash.append(p)), \
             patch.object(dashboard, "find_ableton_app", return_value=None):
            dashboard.on_deck_action({"action": "open", "key": "aurora"})
        self.assertEqual(opened_dash, [str(fallback_set)])

    def test_build_app_includes_removable_volumes_usage_description(self):
        with tempfile.TemporaryDirectory(prefix="tracker-plist-") as tmp:
            dist = Path(tmp)
            icon = dist / "fake.icns"
            icon.write_bytes(b"icns")
            with patch.object(build_app, "DIST_DIR", dist), \
                 patch.object(build_app, "copy_app_source"), \
                 patch.object(build_app, "build_launcher"):
                bundle = build_app.build_bundle("Tracker", build_app.APPS["Tracker"], icon, "-")
            plist_text = (bundle / "Contents" / "Info.plist").read_text(encoding="utf-8")
            self.assertIn("NSRemovableVolumesUsageDescription", plist_text)
            self.assertIn("<key>LSUIElement</key><true/>", plist_text)

    def test_resolve_project_path_probes_external_volumes_with_onlyin(self):
        volumes = self.root / "Volumes"
        ext = volumes / "T7 Shield"
        ext.mkdir(parents=True)
        song = ext / "October 2026 - Check it - 93 bpm  12.4.als"
        song.touch()

        calls = []

        def fake_mdfind(args, capture_output=True, timeout=5):
            calls.append(args)
            res = unittest.mock.Mock()
            res.returncode = 0
            if "-onlyin" in args and args[args.index("-onlyin") + 1] == str(ext):
                res.stdout = f"{song}\0".encode("utf-8")
            else:
                res.stdout = b""
            return res

        with patch.object(projects, "VOLUMES_ROOT", volumes), \
             patch.object(projects.subprocess, "run", side_effect=fake_mdfind):
            resolved = projects.resolve_project_path(self.db, "October 2026 - Check it - 93 bpm 12.4")
        self.assertEqual(resolved, str(song))
        self.assertTrue(any("-onlyin" in c for c in calls))

    def test_resolve_project_path_surfaces_permission_error_from_external_volume(self):
        volumes = self.root / "Volumes"
        ext = volumes / "T7 Shield"
        ext.mkdir(parents=True)

        real_scandir = projects.os.scandir

        def guarded_scandir(path="."):
            if Path(path) == ext:
                raise PermissionError(1, "Operation not permitted", str(path))
            return real_scandir(path)

        with patch.object(projects, "VOLUMES_ROOT", volumes), \
             patch.object(projects.subprocess, "run", side_effect=FileNotFoundError), \
             patch.object(projects.os, "scandir", side_effect=guarded_scandir):
            with self.assertRaises(PermissionError) as ctx:
                projects.resolve_project_path(self.db, "October 2026 - Check it - 93 bpm 12.4")
        self.assertIn("System Settings", str(ctx.exception))

    def test_open_project_surfaces_permission_error(self):
        song = self.root / "Set.als"
        song.touch()
        with patch.object(projects.os, "scandir", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaises(PermissionError) as ctx:
                projects.open_project(str(song), None)
        self.assertIn("System Settings", str(ctx.exception))

    def test_find_ableton_app_checks_subfolders(self):
        def fake_glob(pattern):
            if pattern == "/Applications/*/Ableton Live *.app":
                return ["/Applications/Work/Ableton Live 12 Suite.app"]
            return []

        with patch.object(projects.glob, "glob", side_effect=fake_glob):
            self.assertEqual(projects.find_ableton_app(), "/Applications/Work/Ableton Live 12 Suite.app")

    def test_build_launcher_runs_background_in_process(self):
        with tempfile.TemporaryDirectory(prefix="tracker-launcher-") as tmp:
            macos = Path(tmp)
            captured = []

            def fake_run(cmd, check=True):
                captured.append(Path(cmd[3]).read_text(encoding="utf-8"))
                Path(cmd[5]).touch()

            with patch.object(build_app.subprocess, "run", side_effect=fake_run):
                build_app.build_launcher(macos, "/usr/bin/python3", "/usr/lib/libpython.dylib", "dashboard_window.py")
            c_code = captured[0]
            self.assertNotIn("execv(PYTHON_PATH", c_code)
            self.assertIn("ABLETON_TRACKER_BACKGROUND", c_code)
            self.assertIn("menubar.py", c_code)
            self.assertIn("py_main(2, bg_argv)", c_code)


if __name__ == "__main__":
    unittest.main()
