#!/usr/bin/env python3
"""Native popover integration verification, using display fixtures and no database.

Failure coverage (written before implementation): AppKit/PyObjC setup failures;
status-item anchoring and repeated toggles; stale status/total/conditional action
updates; clipped long status or totals; dark/light rendering; missing keyboard
focus/shortcuts/Escape; dismissal and callback dispatch; native nested project
menus; disposal restoring the original menu. Screenshots and JSON are repeatable
artifacts, not evidence of a real Ableton session or actual project launch.
"""
import argparse
import json
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import AppKit as AK
from Foundation import NSDate, NSRunLoop
import rumps
from menubar_panel import MenuBarPanel


def pump(seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))


def verify(output):
    output.mkdir(parents=True, exist_ok=True)
    app = AK.NSApplication.sharedApplication()
    app.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
    original_menu = AK.NSMenu.alloc().initWithTitle_('Verification fallback')
    status = AK.NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
    status.button().setTitle_('Panel check')
    status.setMenu_(original_menu)
    projects = rumps.MenuItem('Open Project')
    projects.add(rumps.MenuItem('Recent'))
    projects.add(rumps.MenuItem('Evening Sketch', callback=lambda _: calls.append('project')))
    folder = rumps.MenuItem('Pinned Folder')
    folder.add(rumps.MenuItem('Nested.als', callback=lambda _: calls.append('nested')))
    projects.add(folder)
    projects.add(rumps.MenuItem('Customize…', callback=lambda _: calls.append('customize')))
    calls, checks, screenshots = [], [], []
    panel = MenuBarPanel.attach(status, projects._menu, lambda: calls.append('refresh'),
                         {'tracker': lambda: calls.append('tracker'),
                          'ableton': lambda: calls.append('ableton'),
                          'quit': lambda: calls.append('quit')})
    try:
        fixture = dict(status='Working on: Evening Sketch', today='Today: 1¼ / 3h',
                       week='This week: 6 / 15h', run='Streak: 4 days',
                       show_open_ableton=False, state='tracking')
        panel.update(**fixture)
        assert status.menu() is None
        panel.toggle_(None)
        pump()
        assert panel.popover.isShown()
        assert panel.popover.contentViewController().view().window() is not None
        panel.toggle_(None)
        pump()
        assert not panel.popover.isShown()
        checks.append('anchored status action opens and second action closes')
        for appearance in ('dark', 'light'):
            for name, text, state, show in (
                ('tracking', fixture['status'], 'tracking', False),
                ('closed', 'Ableton not running', 'idle', True),
                ('error', 'Error (3): ' + 'Long error and project message ' * 5, 'error', False),
            ):
                panel.update(**(fixture | dict(status=text, state=state, show_open_ableton=show)))
                panel.popover.setAppearance_(AK.NSAppearance.appearanceNamed_(
                    AK.NSAppearanceNameDarkAqua if appearance == 'dark' else AK.NSAppearanceNameAqua))
                panel.toggle_(None)
                pump()
                assert panel.status_label.stringValue() == text
                assert bool(panel.ableton_button.isHidden()) != show
                view = panel.root
                bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
                view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
                path = output / f'{appearance}-{name}.png'
                bitmap.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True)
                screenshots.append(str(path))
                assert panel.tracker_button.frame().origin.y + panel.tracker_button.frame().size.height <= view.bounds().size.height
                panel.close()
                pump()
        checks.append('six native dark/light renders; long error wraps; conditional Ableton action')
        panel.update(**(fixture | dict(today='Today: 123¾ / 150h', week='This week: 987¾ / 1000h', run='Streak: —')))
        assert panel.today_label.stringValue() == '123¾ / 150h'
        assert panel.week_label.stringValue() == '987¾ / 1000h'
        assert panel.run_label.stringValue() == '—'
        assert panel.run_caption.stringValue() == 'Streak'
        checks.append('existing strings retained, including large values and zero Streak')
        panel.toggle_(None)
        pump()
        panel.tracker_button.performClick_(None)
        assert calls[-1] == 'tracker' and not panel.popover.isShown()
        panel.toggle_(None)
        pump()
        panel.quit_button.performClick_(None)
        assert calls[-1] == 'quit' and not panel.popover.isShown()
        panel.update(**(fixture | dict(show_open_ableton=True)))
        panel.toggle_(None)
        pump()
        panel.ableton_button.performClick_(None)
        assert calls[-1] == 'ableton' and not panel.popover.isShown()
        checks.append('native buttons dismiss and dispatch existing action adapters once')
        assert not panel.tracker_button.keyEquivalent() and not panel.quit_button.keyEquivalent()
        for key, modifiers, expected in [('d', AK.NSEventModifierFlagCommand, 'tracker'), ('q', AK.NSEventModifierFlagCommand, 'quit')]:
            panel.toggle_(None)
            pump()
            event = AK.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
                AK.NSEventTypeKeyDown, (0, 0), modifiers, 0, panel.root.window().windowNumber(), None, key, key, False, 0)
            app.sendEvent_(event)
            pump()
            assert calls[-1] != expected and panel.popover.isShown(), f'Cmd-{key} still triggers {expected}'
            panel.close()
            pump()
        panel.toggle_(None)
        pump()
        event = AK.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
            AK.NSEventTypeKeyDown, (0, 0), 0, 0, panel.root.window().windowNumber(), None, '\x1b', '\x1b', False, 53)
        app.sendEvent_(event)
        pump()
        assert not panel.popover.isShown()
        panel.update(**(fixture | dict(show_open_ableton=True)))
        panel.toggle_(None)
        pump()
        assert panel.root.window().firstResponder() == panel.project_button
        for modifiers, expected in ((0, panel.ableton_button), (0, panel.tracker_button), (AK.NSEventModifierFlagShift, panel.ableton_button)):
            event = AK.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
                AK.NSEventTypeKeyDown, (0, 0), modifiers, 0, panel.root.window().windowNumber(), None, '\t', '\t', False, 48)
            app.sendEvent_(event)
            pump()
            assert panel.root.window().firstResponder() == expected
        panel.close()
        checks.append('no command shortcuts; Escape, Tab and shift-Tab still delivered')
        assert projects['Pinned Folder']._menu is folder._menu
        projects['Evening Sketch'].callback(projects['Evening Sketch'])
        folder['Nested.als'].callback(folder['Nested.als'])
        projects['Customize…'].callback(projects['Customize…'])
        assert calls[-3:] == ['project', 'nested', 'customize']
        checks.append('native project tree and callback identity retained; installed interaction requires separate verification')
    finally:
        panel.dispose()
        assert status.menu() == original_menu
        AK.NSStatusBar.systemStatusBar().removeStatusItem_(status)
    checks.append('dispose restores original native menu')
    result = dict(ok=True, macos=platform.mac_ver()[0], checks=checks,
                  screenshots=screenshots, scope='Real AppKit views/events with synthetic display data and recording action adapters; no DB access.')
    (output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('/tmp/ableton-panel-verification'))
    verify(parser.parse_args().output)
