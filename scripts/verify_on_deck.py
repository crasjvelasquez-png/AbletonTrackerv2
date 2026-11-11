#!/usr/bin/env python3
"""End-to-end On Deck check: real song data, real AppKit panel, copied database.

Failure coverage (written before running): the real history yields no songs or
suggestions; dialog titles leak into the picker; picking does not put exactly
one song On Deck; due dates or next tasks do not reach the panel; long titles
clip the stage control; rows overlap the metrics or actions; dark/light render
breaks; keyboard order skips On Deck rows; pick and popup choices dispatch the
wrong song or action; the source database is modified.

The source database is copied with SQLite's backup API and opened read-only.
Artifacts: screenshots and results.json in --output.
"""
import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import AppKit as AK
from Foundation import NSDate, NSRunLoop
import rumps
from menubar_panel import MenuBarPanel
import songs

SOURCE = Path.home() / ".ableton_tracker" / "sessions.db"


def pump(seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_db(source, target):
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def overlaps(a, b):
    return AK.NSIntersectsRect(a.frame(), b.frame())


def verify(output, source):
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    before = digest(source)
    work = Path(tempfile.mkdtemp(prefix="on-deck-"))
    db = work / "sessions.db"
    copy_db(source, db)
    checks, screenshots, calls = [], [], []
    today = date.today()

    loaded = songs.load_songs(db)
    titles = [s["title"] for s in loaded]
    assert loaded, "no songs in copied history"
    assert not any(songs.is_noise(t) for t in titles), "dialog titles leaked into songs"
    multi = [s for s in loaded if len(s["files"]) > 1]
    model = songs.panel_model(loaded, today)
    assert model["mode"] in ("empty", "set")
    checks.append(f"{len(loaded)} songs from {sum(len(s['files']) for s in loaded)} set names; "
                  f"{len(multi)} songs merge stage-split files")

    app = AK.NSApplication.sharedApplication()
    app.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
    status = AK.NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
    status.button().setTitle_("On Deck check")
    status.setMenu_(AK.NSMenu.alloc().initWithTitle_("fallback"))
    projects = rumps.MenuItem("Open Project")
    projects.add(rumps.MenuItem("Recent"))
    record = lambda name: (lambda *args: calls.append((name, *args)))
    actions = {name: record(name) for name in ("tracker", "ableton", "quit", "deck_open", "deck_pick", "deck_stage",
                                                "deck_next_edit", "deck_next_done", "deck_due", "deck_snooze")}
    panel = MenuBarPanel.attach(status, projects._menu, lambda: None, actions)
    base = dict(status="Working on: April 2026 - Prince - 130 bpm Emaj", today="Today: 1¼ / 2h",
                week="This week: 6 / 7h", run="Streak: 4 days", show_open_ableton=False, state="tracking")

    def shoot(name, deck):
        for appearance in ("dark", "light"):
            panel.update(**base, deck=deck)
            panel.popover.setAppearance_(AK.NSAppearance.appearanceNamed_(
                AK.NSAppearanceNameDarkAqua if appearance == "dark" else AK.NSAppearanceNameAqua))
            panel.toggle_(None)
            pump()
            view = panel.root
            bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
            view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
            path = output / f"{appearance}-{name}.png"
            bitmap.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True)
            screenshots.append(str(path))
            assert panel.quit_button.frame().origin.y + 32 <= view.bounds().size.height
            visible = [b for b in panel._focus_buttons if not b.isHidden()]
            for i, a in enumerate(visible):
                for b in visible[i + 1:]:
                    assert not overlaps(a, b), f"{a.title()} overlaps {b.title()}"
            assert panel.today_caption.frame().origin.y > max(b.frame().origin.y + b.frame().size.height
                                                               for b in visible if b in panel.deck_pick_buttons + [
                                                                   panel.deck_change_button, panel.deck_all_button]) \
                if any(b in visible for b in panel.deck_pick_buttons + [panel.deck_change_button, panel.deck_all_button]) else True
            panel.close()
            pump()

    try:
        # 1. Empty deck shows real suggestions; a pick dispatches that song's key.
        with sqlite3.connect(db) as conn:
            songs.ensure_song_schema(conn)
            conn.execute("UPDATE song_state SET on_deck = 0, on_deck_since = NULL")
        conn.close()
        empty = songs.panel_model(songs.load_songs(db), today)
        assert empty["mode"] == "empty" and empty["suggestions"], "no suggestions from real history"
        shoot("empty", empty)
        panel.update(**base, deck=empty)
        panel.toggle_(None)
        pump()
        assert panel.root.window().firstResponder() == panel.deck_pick_buttons[0]
        panel.deck_pick_buttons[0].performClick_(None)
        assert calls[-1] == ("deck_pick", empty["suggestions"][0][0]) and not panel.popover.isShown()
        checks.append("empty state: suggestions " + "; ".join(f"{t} ({s})" for _, t, s in empty["suggestions"]))

        # 2. Pick the first suggestion for real on the copy.
        key = empty["suggestions"][0][0]
        due = songs.put_on_deck(db, key)
        loaded = songs.load_songs(db)
        assert [s["key"] for s in loaded if s["on_deck"]] == [key]
        set_model = songs.panel_model(loaded, today)
        assert set_model["mode"] == "set" and set_model["due"] != "No due date"
        shoot("deck-no-task", set_model)
        checks.append(f"pick sets one On Deck song with proposed due {due}: {set_model['due']}")

        songs.set_next_task(db, key, "write verse 2 lyrics and record a demo vocal over the full arrangement")
        songs.set_due_date(db, key, (today - timedelta(days=3)).isoformat())
        late = songs.panel_model(songs.load_songs(db), today)
        assert late["overdue"] and late["next"].startswith("Next: write verse 2")
        shoot("deck-overdue-task", late)
        panel.update(**base, deck=late)
        assert panel.deck_due_button.tone == "warning"
        assert panel.deck_stage_button.frame().origin.x + panel.deck_stage_button.frame().size.width <= panel.deck_due_button.frame().origin.x
        assert not panel.deck_check_button.isHidden() and panel.root.deck_card is not None
        assert panel.deck_title_button.toolTip() == late["title"]
        checks.append("overdue uses warning tone; stage track and due share a row; done circle shown with a task; full name in tooltip")

        # 3. Keyboard order and actions.
        panel.toggle_(None)
        pump()
        order = [panel.deck_title_button, panel.deck_stage_button, panel.deck_due_button,
                 panel.deck_check_button, panel.deck_next_button, panel.deck_change_button, panel.project_button]
        assert panel.root.window().firstResponder() == order[0]
        for expected in order[1:]:
            event = AK.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
                AK.NSEventTypeKeyDown, (0, 0), 0, 0, panel.root.window().windowNumber(), None, "\t", "\t", False, 48)
            app.sendEvent_(event)
            pump(0.05)
            assert panel.root.window().firstResponder() == expected, f"Tab skipped to {panel.root.window().firstResponder()}"
        panel.deck_title_button.performClick_(None)
        assert calls[-1] == ("deck_open",)
        panel.toggle_(None)
        pump()
        panel.deck_check_button.performClick_(None)
        assert calls[-1] == ("deck_next_done",)
        panel.toggle_(None)
        pump()
        panel.deck_next_button.performClick_(None)
        assert calls[-1] == ("deck_next_edit",)
        checks.append("Tab walks On Deck rows before Open Project; title opens, circle completes, text edits")

        for name, entries_from, expected in (
            ("stage", lambda: [(label, panel._act("deck_stage", stage)) for stage, label in late["stages"]], ("deck_stage", "recording")),
            ("due", lambda: [("In 1 week", panel._act("deck_due", 7))], ("deck_due", 7)),
            ("change", lambda: panel._song_entries(late["more"]), ("deck_pick", late["more"][0][0])),
        ):
            menu = AK.NSMenu.alloc().initWithTitle_("")
            panel._menu_callbacks = []
            panel._fill_menu(menu, entries_from())
            index = 2 if name == "stage" else 0
            panel.menuChoice_(menu.itemAtIndex_(index))
            panel._chosen()
            assert calls[-1] == expected, (name, calls[-1])
        checks.append("stage, due and change menu items dispatch the chosen value")
    finally:
        panel.dispose()
        AK.NSStatusBar.systemStatusBar().removeStatusItem_(status)
        shutil.rmtree(work, ignore_errors=True)

    assert digest(source) == before, "source database changed"
    checks.append("source database unchanged (sha256 before and after)")
    result = dict(ok=True, checks=checks, screenshots=screenshots,
                  scope="Copied real database, real AppKit panel and events, recording action adapters; "
                        "does not launch Live or touch the installed runtime.")
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/ableton-on-deck-verification"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    verify(args.output, args.source)
