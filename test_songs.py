"""On Deck song model tests, written before songs.py.

Failure modes this file guards against:
1. Stage-split files (Mixing / Master / Mastering Session / Stems), Live-version
   resaves ("12.4") and "v2" copies are counted as separate songs.
2. "Remixed" or a title word containing "mix" is mistaken for a Mixing file.
3. Legacy aliases are ignored, or a database without the legacy tables crashes.
4. Live dialog titles ("Save", "menu", "Untitled") appear as songs.
5. File evidence is overwritten by a lower manual stage, or Released does not settle.
6. Two songs end up On Deck, re-picking duplicates state, or a swap drops the
   previous song without snoozing it.
7. Snoozed, settled, tiny-sketch or already-On-Deck songs are suggested, or the
   ordering is nondeterministic.
8. Due-date proposals ignore recent pace, divide by zero with no recent work,
   or produce absurd dates.
9. Panel strings misreport overdue / due today / remaining days.
10. "Open" targets the production file while the song is in mixing.
11. Writes touch the source-of-truth sessions rows.
12. A missing database or missing song tables break reads.
"""
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import songs

DAY = 86400.0


def ts(day: date, hour: int = 20) -> float:
    return datetime(day.year, day.month, day.day, hour).timestamp()


TODAY = date(2026, 11, 10)
NOW = ts(TODAY, 12)


class SongTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sessions.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("""CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT NOT NULL,
                start_time REAL NOT NULL, end_time REAL, active_seconds REAL DEFAULT 0,
                last_seen_time REAL)""")

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name, days_ago, hours):
        start = ts(TODAY - timedelta(days=days_ago))
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO sessions(project_name,start_time,end_time,active_seconds,last_seen_time) VALUES(?,?,?,?,?)",
                (name, start, start + hours * 3600, hours * 3600, start + hours * 3600))

    def legacy(self, aliases=(), metadata=()):
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE project_aliases (alias_name TEXT PRIMARY KEY, canonical_name TEXT NOT NULL)")
            conn.execute("""CREATE TABLE project_metadata (project_name TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)""")
            conn.executemany("INSERT INTO project_aliases VALUES(?,?)", aliases)
            conn.executemany("INSERT INTO project_metadata(project_name,status,due_date,updated_at) VALUES(?,?,?,0)", metadata)

    def load(self, now=NOW):
        return {s["key"]: s for s in songs.load_songs(self.db, now=now)}

    def by_title(self, title, now=NOW):
        return next(s for s in songs.load_songs(self.db, now=now) if s["title"] == title)


class GroupingTests(SongTestCase):
    def test_stage_split_files_and_resaves_form_one_song(self):
        for name in ("Hard pill to swallow - Zenny - Mixing", "Hard pill to swallow - Zenny - Mixing 12.4",
                     "Hard pill to swallow - Zenny - Master"):
            self.add(name, 5, 1)
        self.add("First of the Month - Misa James - Mixing Session", 9, 1)
        self.add("First of the Month - Misa James - Mixing Session Stems", 8, 1)
        self.add("First of the Month - Misa James - Mastering Session", 7, 1)
        self.add("QUERIDA - MAIN PROJECT 2025 12.3 v2", 6, 1)
        self.add("April 2026 - How deep is your love - 97 bpm Emaj", 4, 2)
        self.add("April 2026 - How deep is your love - 97 bpm Emaj - MASTER", 3, 1)
        loaded = songs.load_songs(self.db, now=NOW)
        files = sorted(len(s["files"]) for s in loaded)
        self.assertEqual(files, [1, 2, 3, 3])
        self.assertEqual(songs.song_key("Querida - c4milo MIXING"), songs.song_key("Querida - c4milo - Mastering Session"))
        self.assertEqual(songs.song_key("blessed 87 bpm Emin - c4milo 12.3"), songs.song_key("blessed 87 bpm Emin - c4milo"))
        self.assertEqual(songs.song_key("how it be - Zenny - Master Session"), songs.song_key("how it be - Zenny - Mixing Session"))

    def test_mix_word_detection_is_word_based(self):
        self.assertIsNone(songs.file_stage("Wandered - Remixed 2026 - Misa James"))
        self.assertIsNone(songs.file_stage("Mixtape vibes - 90 bpm"))
        self.assertEqual(songs.file_stage("forest mixing post freeze"), "mixing")
        self.assertEqual(songs.file_stage("Querida - c4milo MIXING"), "mixing")
        self.assertEqual(songs.file_stage("Wonder Woman - Zenny - Master"), "mastering")
        self.assertEqual(songs.file_stage("TOMORROW - Teo - Mastering Session"), "mastering")
        self.assertNotEqual(songs.song_key("Wandered - Remixed 2026 - Misa James"), songs.song_key("Wandered - 2025"))

    def test_different_artists_stay_separate(self):
        self.add("Talk about it - Zenny - Mixing", 3, 1)
        self.add("Talk about it - Misa James - Mixing", 3, 1)
        self.assertEqual(len(songs.load_songs(self.db, now=NOW)), 2)

    def test_legacy_aliases_merge_and_missing_tables_are_fine(self):
        self.add("QUERIDA - MAIN PROJECT 2025 12.3 v2", 6, 3)
        self.add("Querida - c4milo MIXING", 5, 2)
        self.assertEqual(len(songs.load_songs(self.db, now=NOW)), 2)
        self.legacy(aliases=[("Querida - c4milo MIXING", "QUERIDA - MAIN PROJECT 2025 12.3 v2")])
        loaded = songs.load_songs(self.db, now=NOW)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["stage"], "mixing")

    def test_dialog_titles_are_not_songs(self):
        for name in ("Save", "menu", "Message Manager_Popup", "Input Config", "Save Synplant Patch",
                     "Untitled", "Untitled Project", "Bounce...", " "):
            self.add(name, 1, 2)
        self.add("long drive", 1, 1)
        self.assertEqual([s["title"] for s in songs.load_songs(self.db, now=NOW)], ["long drive"])

    def test_missing_database_and_tables_read_empty(self):
        self.assertEqual(songs.load_songs(Path(self.tmp.name) / "nope.db", now=NOW), [])
        self.assertEqual(songs.load_songs(self.db, now=NOW), [])


class StageTests(SongTestCase):
    def test_inferred_stage_and_manual_override(self):
        self.add("ufo Emin 125 bpm - c4milo", 2, 0.2)
        self.add("April 2026 - Prince - 130 bpm Emaj", 2, 5)
        self.add("Neighbors - Zenny - Mixing", 2, 2)
        self.assertEqual(self.by_title("ufo Emin 125 bpm - c4milo")["stage"], "idea")
        prince = self.by_title("April 2026 - Prince - 130 bpm Emaj")
        self.assertEqual(prince["stage"], "production")
        songs.set_stage(self.db, prince["key"], "recording", now=NOW)
        self.assertEqual(self.by_title("April 2026 - Prince - 130 bpm Emaj")["stage"], "recording")
        neighbors = self.by_title("Neighbors - Zenny - Mixing")
        songs.set_stage(self.db, neighbors["key"], "production", now=NOW)
        self.assertEqual(self.by_title("Neighbors - Zenny - Mixing")["stage"], "mixing")
        with self.assertRaises(ValueError):
            songs.set_stage(self.db, prince["key"], "vibing", now=NOW)

    def test_released_settles_and_leaves_deck(self):
        self.add("long drive", 2, 3)
        key = self.by_title("long drive")["key"]
        songs.put_on_deck(self.db, key, now=NOW)
        songs.set_stage(self.db, key, "released", now=NOW)
        song = self.by_title("long drive")
        self.assertTrue(song["settled"])
        self.assertFalse(song["on_deck"])
        self.assertIsNone(songs.current_deck(songs.load_songs(self.db, now=NOW)))

    def test_open_target_follows_stage(self):
        self.add("Fall this way - Zenny - Mixing", 10, 2)
        self.add("Fall this way - Zenny", 1, 1)
        self.assertEqual(self.by_title("Fall this way - Zenny")["open_name"], "Fall this way - Zenny - Mixing")
        self.add("Fall this way - Zenny - Master", 5, 1)
        self.assertEqual(self.by_title("Fall this way - Zenny")["open_name"], "Fall this way - Zenny - Master")


class DeckTests(SongTestCase):
    def setUp(self):
        super().setUp()
        self.add("April 2026 - Prince - 130 bpm Emaj", 3, 4)
        self.add("August 2026 - DANCE - 118 bpm", 4, 4)
        self.add("September 2026 - Colorful - 138 bpm", 5, 4)

    def keys(self):
        return {s["title"]: s["key"] for s in songs.load_songs(self.db, now=NOW)}

    def test_only_one_song_on_deck_and_swap_snoozes_previous(self):
        k = self.keys()
        songs.put_on_deck(self.db, k["April 2026 - Prince - 130 bpm Emaj"], now=NOW)
        songs.put_on_deck(self.db, k["April 2026 - Prince - 130 bpm Emaj"], now=NOW + 60)
        songs.put_on_deck(self.db, k["August 2026 - DANCE - 118 bpm"], now=NOW + 120, snooze_previous_days=7)
        loaded = songs.load_songs(self.db, now=NOW + 180)
        self.assertEqual([s["title"] for s in loaded if s["on_deck"]], ["August 2026 - DANCE - 118 bpm"])
        prince = next(s for s in loaded if s["title"].startswith("April"))
        self.assertEqual(prince["snoozed_until"], (TODAY + timedelta(days=7)).isoformat())
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM song_state WHERE on_deck=1").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*), sum(active_seconds) FROM sessions").fetchone(), (3, 12 * 3600))

    def test_put_on_deck_sets_proposed_due_date_once(self):
        k = self.keys()
        due = songs.put_on_deck(self.db, k["April 2026 - Prince - 130 bpm Emaj"], now=NOW)
        self.assertGreaterEqual(date.fromisoformat(due), TODAY + timedelta(days=3))
        songs.set_due_date(self.db, k["April 2026 - Prince - 130 bpm Emaj"], (TODAY + timedelta(days=30)).isoformat(), now=NOW)
        songs.put_on_deck(self.db, k["April 2026 - Prince - 130 bpm Emaj"], now=NOW + 60)
        self.assertEqual(self.by_title("April 2026 - Prince - 130 bpm Emaj")["due_date"], (TODAY + timedelta(days=30)).isoformat())

    def test_snoozed_songs_return_after_date(self):
        k = self.keys()
        songs.put_on_deck(self.db, k["April 2026 - Prince - 130 bpm Emaj"], now=NOW)
        songs.snooze_deck(self.db, days=7, now=NOW)
        titles = [s["title"] for s in songs.suggestions(songs.load_songs(self.db, now=NOW), today=TODAY)]
        self.assertNotIn("April 2026 - Prince - 130 bpm Emaj", titles)
        later = NOW + 8 * DAY
        titles = [s["title"] for s in songs.suggestions(songs.load_songs(self.db, now=later), today=TODAY + timedelta(days=8))]
        self.assertIn("April 2026 - Prince - 130 bpm Emaj", titles)

    def test_suggestions_order_and_exclusions(self):
        self.add("tiny sketch", 1, 0.1)
        self.add("Hot Shot - Zenny - Mixing Session", 1, 3)
        self.add("Old Song - Zenny - Master", 1, 1)
        self.legacy(metadata=[("Hot Shot - Zenny - Mixing Session", "finished", ""),
                              ("September 2026 - Colorful - 138 bpm", "", (TODAY + timedelta(days=9)).isoformat())])
        k = self.keys()
        songs.set_due_date(self.db, k["August 2026 - DANCE - 118 bpm"], (TODAY + timedelta(days=2)).isoformat(), now=NOW)
        titles = [s["title"] for s in songs.suggestions(songs.load_songs(self.db, now=NOW), today=TODAY, limit=5)]
        self.assertEqual(titles[:3], ["August 2026 - DANCE - 118 bpm", "September 2026 - Colorful - 138 bpm",
                                      "Old Song - Zenny - Master"])
        self.assertNotIn("tiny sketch", titles)
        self.assertNotIn("Hot Shot - Zenny - Mixing Session", titles)
        songs.put_on_deck(self.db, k["August 2026 - DANCE - 118 bpm"], now=NOW)
        titles = [s["title"] for s in songs.suggestions(songs.load_songs(self.db, now=NOW), today=TODAY, limit=2)]
        self.assertEqual(len(titles), 2)
        self.assertNotIn("August 2026 - DANCE - 118 bpm", titles)

    def test_next_task_done_and_events(self):
        k = self.keys()["April 2026 - Prince - 130 bpm Emaj"]
        songs.put_on_deck(self.db, k, now=NOW)
        songs.set_next_task(self.db, k, "  write verse 2 lyrics  ", now=NOW)
        self.assertEqual(self.by_title("April 2026 - Prince - 130 bpm Emaj")["next_task"], "write verse 2 lyrics")
        songs.complete_next_task(self.db, k, now=NOW + 60)
        self.assertEqual(self.by_title("April 2026 - Prince - 130 bpm Emaj")["next_task"], "")
        with sqlite3.connect(self.db) as conn:
            kinds = [r[0] for r in conn.execute("SELECT kind FROM song_events WHERE song_key=? ORDER BY id", (k,))]
        self.assertEqual(kinds, ["on_deck", "due_date", "next_task", "task_done"])


class DueDateTests(SongTestCase):
    def test_proposal_uses_recent_pace_and_clamps(self):
        self.add("Talk about it - Zenny - Mixing", 3, 2)
        song = self.by_title("Talk about it - Zenny - Mixing")
        due, why = songs.propose_due_date(songs.load_songs(self.db, now=NOW), song, today=TODAY)
        # Default mix estimate 4h, 2h done, pace 2h / 4 weeks = 0.5h/week -> 1h/week floor -> 14 days.
        self.assertEqual(due, TODAY + timedelta(days=14))
        self.assertIn("h/week", why)
        for day in range(1, 28, 2):
            self.add("Busy - c4milo", day, 3)
        due, _ = songs.propose_due_date(songs.load_songs(self.db, now=NOW), self.by_title("Talk about it - Zenny - Mixing"), today=TODAY)
        self.assertEqual(due, TODAY + timedelta(days=3))

    def test_mix_estimate_uses_finished_mix_evidence(self):
        for name, hours in (("A - Zenny - Mixing", 3), ("B - Zenny - Mixing", 5), ("C - Zenny - Mixing", 7)):
            self.add(name, 20, hours)
            self.add(name.replace("Mixing", "Master"), 19, 0.5)
        self.assertEqual(songs.stage_estimate_hours(songs.load_songs(self.db, now=NOW), "mixing"), 5)
        self.assertEqual(songs.stage_estimate_hours([], "mixing"), songs.DEFAULT_STAGE_HOURS["mixing"])

    def test_no_activity_does_not_divide_by_zero(self):
        song = {"key": "x", "stage": "production", "stage_seconds": 0, "files": []}
        due, _ = songs.propose_due_date([], song, today=TODAY)
        self.assertEqual(due, TODAY + timedelta(days=42))


class PanelModelTests(unittest.TestCase):
    def song(self, **kw):
        base = dict(key="k", title="Prince", stage="recording", next_task="", due_date="", on_deck=True)
        return base | kw

    def test_due_strings(self):
        m = songs.deck_display(self.song(due_date=(TODAY + timedelta(days=14)).isoformat()), today=TODAY)
        self.assertEqual(m["stage_label"], "Recording")
        self.assertEqual(m["due"], "Due Nov 24 · 14 days left")
        self.assertFalse(m["overdue"])
        self.assertEqual(songs.deck_display(self.song(due_date=TODAY.isoformat()), today=TODAY)["due"], "Due today")
        self.assertEqual(songs.deck_display(self.song(due_date=(TODAY + timedelta(days=1)).isoformat()), today=TODAY)["due"], "Due tomorrow")
        late = songs.deck_display(self.song(due_date=(TODAY - timedelta(days=3)).isoformat()), today=TODAY)
        self.assertEqual(late["due"], "Overdue by 3 days")
        self.assertTrue(late["overdue"])
        self.assertEqual(songs.deck_display(self.song(), today=TODAY)["due"], "No due date")

    def test_display_fields(self):
        m = songs.deck_display(self.song(title="April 2026 - Prince - 130 bpm Emaj 12.4",
                                         due_date=(TODAY + timedelta(days=17)).isoformat()), today=TODAY)
        self.assertEqual(m["display_title"], "April 2026 - Prince - 130 bpm Emaj")
        self.assertEqual((m["due_primary"], m["due_secondary"]), ("Due Nov 27", "17 days left"))
        self.assertEqual(m["stage_index"], 2)
        self.assertEqual(m["task"], "")
        late = songs.deck_display(self.song(due_date=(TODAY - timedelta(days=1)).isoformat(), next_task="lyrics"), today=TODAY)
        self.assertEqual((late["due_primary"], late["due_secondary"]), ("Overdue", "by 1 day"))
        self.assertEqual(late["task"], "lyrics")
        today = songs.deck_display(self.song(due_date=TODAY.isoformat()), today=TODAY)
        self.assertEqual((today["due_primary"], today["due_secondary"]), ("Due today", "Nov 10"))
        none = songs.deck_display(self.song(), today=TODAY)
        self.assertEqual((none["due_primary"], none["due_secondary"]), ("No due date", "Set one"))
        self.assertEqual(songs.display_title("Hard pill to swallow - Zenny - Mixing 12.4"), "Hard pill to swallow - Zenny - Mixing")
        self.assertEqual(songs.display_title("May 11 - Gas - 90 bpm Bbmin"), "May 11 - Gas - 90 bpm Bbmin")

    def test_next_task_strings(self):
        self.assertEqual(songs.deck_display(self.song(), today=TODAY)["next"], "Add a next task…")
        self.assertEqual(songs.deck_display(self.song(next_task="verse 2 lyrics"), today=TODAY)["next"], "Next: verse 2 lyrics")


if __name__ == "__main__":
    unittest.main()
