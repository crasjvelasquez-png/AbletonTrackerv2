"""Native menu-bar presentation. Owns no tracking, persistence or app processes."""
from __future__ import annotations

import logging

import AppKit as AK
import objc
from Foundation import NSMakeRect, NSMakeSize, NSPoint

def _dynamic(role):
    """Use the same semantic colors as the surrounding macOS interface."""
    return {
        'background': AK.NSColor.windowBackgroundColor,
        'text': AK.NSColor.labelColor,
        'secondary': AK.NSColor.secondaryLabelColor,
        'line': AK.NSColor.separatorColor,
        'accent': AK.NSColor.controlAccentColor,
        'hover': AK.NSColor.quaternaryLabelColor,
        'focus': AK.NSColor.keyboardFocusIndicatorColor,
        'warning': AK.NSColor.systemOrangeColor,
    }[role]()


def _font(size, weight=AK.NSFontWeightRegular, numeric=False):
    if numeric:
        return AK.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
    return AK.NSFont.systemFontOfSize_weight_(size, weight)


def _rounded(rect, radius, fill, stroke=None):
    path = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
    fill.setFill()
    path.fill()
    if stroke:
        stroke.setStroke()
        path.setLineWidth_(0.5)
        path.stroke()


class PanelRoot(AK.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        _dynamic('background').setFill()
        AK.NSRectFill(self.bounds())
        card = getattr(self, 'deck_card', None)
        if card is not None:
            # A quiet grouped surface: same material as the panel, slightly lifted.
            _rounded(card, 10, _dynamic('text').colorWithAlphaComponent_(0.045),
                     _dynamic('line'))
        if not hasattr(self, 'separator_ys'):
            return
        _dynamic('line').setStroke()
        path = AK.NSBezierPath.bezierPath()
        for y in self.separator_ys:
            path.moveToPoint_((16, y))
            path.lineToPoint_((304, y))
        path.moveToPoint_((160, self.metrics_y + 2))
        path.lineToPoint_((160, self.metrics_y + 56))
        path.setLineWidth_(0.5)
        path.stroke()
        role = 'warning' if self.display_state == 'error' else 'accent' if self.display_state == 'tracking' else 'secondary'
        _dynamic(role).setFill()
        AK.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(294, 23, 6, 6)).fill()

    def cancelOperation_(self, sender):
        self.panel_controller.close()

    def viewDidChangeEffectiveAppearance(self):
        self.setNeedsDisplay_(True)
        for view in self.subviews():
            view.setNeedsDisplay_(True)


class PanelButton(AK.NSButton):
    def acceptsFirstResponder(self):
        return True

    def canBecomeKeyView(self):
        return self.isEnabled() and not self.isHiddenOrHasHiddenAncestor()

    def becomeFirstResponder(self):
        result = objc.super(PanelButton, self).becomeFirstResponder()
        self.setNeedsDisplay_(True)
        return result

    def resignFirstResponder(self):
        result = objc.super(PanelButton, self).resignFirstResponder()
        self.setNeedsDisplay_(True)
        return result

    def initWithFrame_(self, frame):
        self = objc.super(PanelButton, self).initWithFrame_(frame)
        if self is not None:
            self.primary = False
            self.tone = 'text'
            self.title_font = None
            self.hovered = False
            self.shortcut = ''
            self.shows_keyboard_focus = False
            self.setBordered_(False)
            self.setButtonType_(AK.NSButtonTypeMomentaryPushIn)
            self.setFocusRingType_(AK.NSFocusRingTypeNone)
        return self

    def updateTrackingAreas(self):
        objc.super(PanelButton, self).updateTrackingAreas()
        for area in self.trackingAreas():
            self.removeTrackingArea_(area)
        self.addTrackingArea_(AK.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), AK.NSTrackingMouseEnteredAndExited | AK.NSTrackingActiveAlways | AK.NSTrackingInVisibleRect,
            self, None))

    def mouseEntered_(self, event):
        self.hovered = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hovered = False
        self.setNeedsDisplay_(True)

    @objc.python_method
    def paints_hover(self):
        return True

    @objc.python_method
    def draw_content(self):
        foreground = _dynamic(self.tone)
        style = AK.NSMutableParagraphStyle.alloc().init()
        style.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
        font = self.title_font or _font(13, AK.NSFontWeightMedium if self.primary else AK.NSFontWeightRegular)
        attrs = {AK.NSFontAttributeName: font,
                 AK.NSForegroundColorAttributeName: foreground, AK.NSParagraphStyleAttributeName: style}
        title = AK.NSAttributedString.alloc().initWithString_attributes_(self.title(), attrs)
        hint_width = 0
        if self.shortcut:
            hint = AK.NSAttributedString.alloc().initWithString_attributes_(self.shortcut, {
                AK.NSFontAttributeName: _font(12), AK.NSForegroundColorAttributeName: _dynamic('secondary')})
            hint_width = hint.size().width + 12
            hint.drawAtPoint_((self.bounds().size.width - hint.size().width - 8,
                               (self.bounds().size.height - hint.size().height) / 2))
        height = title.size().height
        title.drawInRect_(NSMakeRect(8, (self.bounds().size.height - height) / 2,
                                     max(0, self.bounds().size.width - 16 - hint_width), height))

    def drawRect_(self, rect):
        box = AK.NSInsetRect(self.bounds(), 1, 1)
        if (self.hovered or self.isHighlighted()) and self.paints_hover():
            background = _dynamic('hover')
            if self.isHighlighted():
                background = background.blendedColorWithFraction_ofColor_(0.08, _dynamic('text'))
            _rounded(box, 6, background)
        self.draw_content()
        if self.shows_keyboard_focus and self.window() and self.window().firstResponder() == self:
            path = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AK.NSInsetRect(box, 1, 1), 5, 5)
            _dynamic('focus').setStroke()
            path.setLineWidth_(1.5)
            path.stroke()


def _text(string, size, weight=AK.NSFontWeightRegular, role='text', align=AK.NSTextAlignmentLeft):
    style = AK.NSMutableParagraphStyle.alloc().init()
    style.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
    style.setAlignment_(align)
    return AK.NSAttributedString.alloc().initWithString_attributes_(string, {
        AK.NSFontAttributeName: _font(size, weight), AK.NSForegroundColorAttributeName: _dynamic(role),
        AK.NSParagraphStyleAttributeName: style})


class StageTrackButton(PanelButton):
    """Current stage plus a segmented track of the song pipeline; opens the stage menu."""
    @objc.python_method
    def draw_content(self):
        index, count = getattr(self, 'stage_index', 0), getattr(self, 'stage_count', 5)
        width = self.bounds().size.width
        label = _text(self.title(), 12, AK.NSFontWeightSemibold)
        step = _text(f'{min(index + 1, count)} of {count}' if index < count else 'Done', 11, role='secondary')
        step_width = step.size().width
        label.drawInRect_(NSMakeRect(8, 6, width - 28 - step_width, label.size().height))
        step.drawAtPoint_((width - 8 - step_width, 7))
        gap, left, top = 3, 8, self.bounds().size.height - 12
        segment = (width - 16 - gap * (count - 1)) / count
        for i in range(count):
            role = 'accent' if i <= index else 'line'
            _rounded(NSMakeRect(left + i * (segment + gap), top, segment, 4), 2, _dynamic(role))


class DueButton(PanelButton):
    """Two right-aligned lines: the date state, then the remaining time."""
    @objc.python_method
    def draw_content(self):
        width, height = self.bounds().size.width, self.bounds().size.height
        primary = _text(getattr(self, 'primary_text', ''), 12, AK.NSFontWeightSemibold,
                        'warning' if self.tone == 'warning' else 'text', AK.NSTextAlignmentRight)
        secondary = _text(getattr(self, 'secondary_text', ''), 11, role='warning' if self.tone == 'warning' else 'secondary',
                          align=AK.NSTextAlignmentRight)
        top = (height - primary.size().height - secondary.size().height) / 2
        primary.drawInRect_(NSMakeRect(8, top, width - 16, primary.size().height))
        secondary.drawInRect_(NSMakeRect(8, top + primary.size().height, width - 16, secondary.size().height))


class CheckButton(PanelButton):
    """Reminders-style completion circle; the checkmark previews on hover."""
    @objc.python_method
    def draw_content(self):
        size = 16
        rect = NSMakeRect((self.bounds().size.width - size) / 2, (self.bounds().size.height - size) / 2, size, size)
        active = self.hovered or self.isHighlighted()
        circle = AK.NSBezierPath.bezierPathWithOvalInRect_(AK.NSInsetRect(rect, 0.75, 0.75))
        circle.setLineWidth_(1.5)
        _dynamic('accent' if active else 'secondary').setStroke()
        circle.stroke()
        if active:
            mark = AK.NSBezierPath.bezierPath()
            x, y = rect.origin.x, rect.origin.y
            mark.moveToPoint_((x + 4.5, y + 8.5))
            mark.lineToPoint_((x + 7, y + 11))
            mark.lineToPoint_((x + 11.5, y + 5.5))
            mark.setLineWidth_(1.6)
            mark.setLineCapStyle_(AK.NSLineCapStyleRound)
            mark.setLineJoinStyle_(AK.NSLineJoinStyleRound)
            _dynamic('accent').setStroke()
            mark.stroke()

    @objc.python_method
    def paints_hover(self):
        return False


class MenuBarPanel(AK.NSObject):
    """Attach to an existing rumps status item; keep the original menu as fallback."""
    @classmethod
    @objc.python_method
    def attach(cls, status_item, project_menu, refresh, actions):
        self = cls.alloc().init()
        try:
            self._configure(status_item, project_menu, refresh, actions)
        except Exception:
            if hasattr(self, 'popover'):
                self.dispose()
            raise
        return self

    @objc.python_method
    def _configure(self, status_item, project_menu, refresh, actions):
        self.status_item = status_item
        self.project_menu = project_menu
        self.refresh = refresh
        self.actions = actions
        self.original_menu = status_item.menu()
        self.original_target = status_item.button().target()
        self.original_action = status_item.button().action()
        self._key_monitor = None
        self._menu_open = False
        self._disposed = False
        self._display = None
        self._focus_buttons = []
        self.root = PanelRoot.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 360))
        self.root.panel_controller = self
        self.root.display_state = 'idle'
        self.header = self._label('Ableton Tracker', 14, AK.NSFontWeightSemibold)
        self.status_label = self._label('', 13, secondary=True)
        self.status_label.setLineBreakMode_(AK.NSLineBreakByWordWrapping)
        self.status_label.cell().setWraps_(True)
        self.status_label.cell().setScrollable_(False)
        self.today_caption = self._label('Today', 12, secondary=True)
        self.week_caption = self._label('This week', 12, secondary=True)
        self.today_label = self._label('', 24, AK.NSFontWeightSemibold)
        self.week_label = self._label('', 24, AK.NSFontWeightSemibold)
        self.run_caption = self._label('Streak', 13, secondary=True)
        self.run_label = self._label('', 13, AK.NSFontWeightMedium)
        self.run_label.setAlignment_(AK.NSTextAlignmentRight)
        self._deck = None
        self._menu_callbacks = []
        self._chosen = None
        self.deck_caption = self._label('On Deck', 12, AK.NSFontWeightSemibold, secondary=True)
        self.deck_hint = self._label("Pick what you're finishing", 13, secondary=True)
        self.deck_title_button = self._button('', 'deckOpen:', 'Open ›', primary=True)
        self.deck_title_button.title_font = _font(14, AK.NSFontWeightSemibold)
        self.deck_stage_button = self._button('', 'deckStage:', cls=StageTrackButton)
        self.deck_due_button = self._button('', 'deckDue:', cls=DueButton)
        self.deck_check_button = self._button('', 'deckDone:', cls=CheckButton)
        self.deck_next_button = self._button('', 'deckNext:')
        self.deck_change_button = self._button('Change On Deck…', 'deckChange:', '›', tone='secondary')
        self.deck_pick_buttons = [self._button('', 'deckPick:') for _ in range(3)]
        for index, button in enumerate(self.deck_pick_buttons):
            button.setTag_(index)
        self.deck_all_button = self._button('All songs…', 'deckAll:', '›', tone='secondary')
        self.project_button = self._button('Open Project', 'showProjects:', '›')
        self.ableton_button = self._button('Open Ableton', 'openAbleton:')
        self.tracker_button = self._button('Open Tracker', 'openTracker:', primary=True)
        self.quit_button = self._button('Quit', 'quitApp:')
        self.scroller = AK.NSScrollView.alloc().initWithFrame_(self.root.frame())
        self.scroller.setDrawsBackground_(False)
        self.scroller.setHasVerticalScroller_(True)
        self.scroller.setAutohidesScrollers_(True)
        self.scroller.setBorderType_(AK.NSNoBorder)
        self.scroller.setDocumentView_(self.root)
        self.view_controller = AK.NSViewController.alloc().init()
        self.view_controller.setView_(self.scroller)
        self.popover = AK.NSPopover.alloc().init()
        self.popover.setContentViewController_(self.view_controller)
        self.popover.setBehavior_(AK.NSPopoverBehaviorTransient)
        self.popover.setAnimates_(False)
        self.popover.setDelegate_(self)
        # Complete all setup before detaching the working fallback menu.
        self._key_monitor = AK.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AK.NSEventMaskKeyDown, self._handle_key)
        status_item.setMenu_(None)
        status_item.button().setTarget_(self)
        status_item.button().setAction_('toggle:')

    @objc.python_method
    def _label(self, text, size, weight=AK.NSFontWeightRegular, secondary=False):
        label = AK.NSTextField.labelWithString_(text)
        label.setFont_(_font(size, weight))
        label.setTextColor_(_dynamic('secondary' if secondary else 'text'))
        label.setSelectable_(False)
        label.setAccessibilityLabel_(text)
        self.root.addSubview_(label)
        return label

    @objc.python_method
    def _button(self, title, action, shortcut='', primary=False, tone='text', cls=None):
        button = (cls or PanelButton).alloc().initWithFrame_(NSMakeRect(0, 0, 100, 42))
        button.setTitle_(title)
        button.tone = tone
        button.setTarget_(self)
        button.setAction_(action)
        button.setAccessibilityLabel_(title)
        button.primary = primary
        button.shortcut = shortcut
        self.root.addSubview_(button)
        return button

    @objc.python_method
    def update(self, *, status, today, week, run, show_open_ableton, state, deck=None):
        display = (status, today, week, run, show_open_ableton, state, repr(deck))
        if display == self._display:
            return
        self._display = display
        self.root.display_state = state
        self.status_label.setStringValue_(status)
        self.status_label.setAccessibilityLabel_(status)
        self.today_label.setStringValue_(today.removeprefix('Today: '))
        self.week_label.setStringValue_(week.removeprefix('This week: '))
        self.run_label.setStringValue_(run.removeprefix('Streak: '))
        for label, full in ((self.today_label, today), (self.week_label, week), (self.run_label, run)):
            label.setAccessibilityLabel_(full)
        self.ableton_button.setHidden_(not show_open_ableton)
        status_height = max(20, self.status_label.cell().cellSizeForBounds_(NSMakeRect(0, 0, 288, 10000)).height + 2)
        self.header.setFrame_(NSMakeRect(16, 16, 260, 22))
        self.status_label.setFrame_(NSMakeRect(16, 43, 288, status_height))
        self._deck = deck
        deck_buttons, deck_bottom = self._layout_deck(deck, 43 + status_height + 14)
        metrics_y = (deck_bottom if deck else 43 + status_height) + 16
        self.root.metrics_y = metrics_y
        self.root.separator_ys = [metrics_y - 8] if deck else []
        for caption, value, x in ((self.today_caption, self.today_label, 16), (self.week_caption, self.week_label, 176)):
            caption.setFrame_(NSMakeRect(x, metrics_y, 128, 18))
            value.setFrame_(NSMakeRect(x, metrics_y + 22, 128, 28))
            self._fit_total(value, 128)
        self.run_caption.setFrame_(NSMakeRect(16, metrics_y + 70, 90, 19))
        self.run_label.setFrame_(NSMakeRect(126, metrics_y + 70, 178, 19))
        self.root.separator_ys.append(metrics_y + 103)
        y = metrics_y + 112
        self.project_button.setFrame_(NSMakeRect(8, y, 304, 32))
        y += 34
        if show_open_ableton:
            self.ableton_button.setFrame_(NSMakeRect(8, y, 304, 32))
            y += 34
        self.tracker_button.setFrame_(NSMakeRect(8, y, 304, 32))
        self.root.separator_ys.append(y + 39)
        self.quit_button.setFrame_(NSMakeRect(8, y + 46, 304, 32))
        height = y + 88
        self.root.setFrameSize_(NSMakeSize(320, height))
        screen = self.status_item.button().window().screen() or AK.NSScreen.mainScreen()
        available = max(160, screen.visibleFrame().size.height - 50)
        viewport = min(height, available)
        self.scroller.setFrameSize_(NSMakeSize(320, viewport))
        self.popover.setContentSize_(NSMakeSize(320, viewport))
        buttons = deck_buttons + [self.project_button] + ([self.ableton_button] if show_open_ableton else []) + [self.tracker_button, self.quit_button]
        self._focus_buttons = buttons
        for index, button in enumerate(buttons):
            button.setNextKeyView_(buttons[(index + 1) % len(buttons)])
        self.root.setNeedsDisplay_(True)

    @objc.python_method
    def _layout_deck(self, deck, y):
        """Place the On Deck card; returns visible buttons in keyboard order and the section bottom."""
        deck_views = [self.deck_caption, self.deck_hint, self.deck_title_button, self.deck_stage_button,
                      self.deck_due_button, self.deck_check_button, self.deck_next_button,
                      self.deck_change_button, self.deck_all_button, *self.deck_pick_buttons]
        for view in deck_views:
            view.setHidden_(True)
        self.root.deck_card = None
        if not deck:
            return [], y
        self.deck_caption.setHidden_(False)
        self.deck_caption.setFrame_(NSMakeRect(16, y, 200, 18))
        y += 24
        visible = []

        def show(button, frame):
            button.setFrame_(frame)
            button.setHidden_(False)
            button.setNeedsDisplay_(True)
            visible.append(button)

        if deck.get('mode') == 'set':
            title, full, stage = deck.get('display_title') or deck['title'], deck['title'], deck['stage_label']
            card_top = y
            self.deck_title_button.setTitle_(title)
            self.deck_title_button.setToolTip_(full)
            self.deck_title_button.setAccessibilityLabel_(f'Open {full} in Live')
            show(self.deck_title_button, NSMakeRect(12, y + 4, 296, 34))
            y += 40
            track = self.deck_stage_button
            track.setTitle_(stage)
            track.stage_index, track.stage_count = deck.get('stage_index', 0), deck.get('stage_count', 5)
            track.setToolTip_('Change stage')
            track.setAccessibilityLabel_(f'Stage: {stage}, step {min(track.stage_index + 1, track.stage_count)} of {track.stage_count}. Change stage')
            show(track, NSMakeRect(12, y, 180, 40))
            due = self.deck_due_button
            due.setTitle_(deck['due'])
            due.primary_text, due.secondary_text = deck.get('due_primary', deck['due']), deck.get('due_secondary', '')
            due.tone = 'warning' if deck.get('overdue') else 'text'
            due.setToolTip_('Change due date')
            due.setAccessibilityLabel_(f"{deck['due']}. Change due date")
            show(due, NSMakeRect(196, y, 112, 40))
            y += 44
            task = deck.get('task', '')
            if task:
                self.deck_check_button.setTitle_('')
                self.deck_check_button.setToolTip_('Mark done')
                self.deck_check_button.setAccessibilityLabel_(f'Mark done: {task}')
                show(self.deck_check_button, NSMakeRect(12, y, 30, 32))
            nxt = self.deck_next_button
            nxt.setTitle_(task or 'Add the next task…')
            nxt.tone = 'text' if task else 'secondary'
            nxt.shortcut = '' if task else '+'
            nxt.setToolTip_(task or 'Add the next task')
            nxt.setAccessibilityLabel_(f'Edit next task: {task}' if task else 'Add the next task')
            show(nxt, NSMakeRect(42, y, 266, 32) if task else NSMakeRect(12, y, 296, 32))
            y += 36
            self.root.deck_card = NSMakeRect(8, card_top, 304, y - card_top)
            show(self.deck_change_button, NSMakeRect(8, y + 6, 304, 30))
            y += 36
        else:
            suggestions = deck.get('suggestions') or []
            self.deck_hint.setHidden_(False)
            self.deck_hint.setStringValue_("Pick what you're finishing" if suggestions or deck.get('all')
                                           else 'Songs appear after you work on a saved set')
            self.deck_hint.setFrame_(NSMakeRect(16, y - 4, 288, 18))
            y += 20
            if suggestions:
                card_top = y
                y += 4
                for button, (key, title, stage) in zip(self.deck_pick_buttons, suggestions):
                    button.setTitle_(title)
                    button.setToolTip_(title)
                    button.shortcut = stage
                    button.setAccessibilityLabel_(f'Put {title} On Deck and open it. Stage: {stage}')
                    show(button, NSMakeRect(12, y, 296, 32))
                    y += 34
                y += 2
                self.root.deck_card = NSMakeRect(8, card_top, 304, y - card_top)
            if deck.get('all'):
                show(self.deck_all_button, NSMakeRect(8, y + 6, 304, 30))
                y += 36
        return visible, y

    @objc.python_method
    def _popup(self, sender, entries):
        """Native menu under a panel row; runs the chosen callback after it closes."""
        menu = AK.NSMenu.alloc().initWithTitle_('')
        menu.setAutoenablesItems_(False)
        self._menu_callbacks = []
        self._fill_menu(menu, entries)
        self._menu_open = True
        self._chosen = None
        self.popover.setBehavior_(AK.NSPopoverBehaviorApplicationDefined)
        try:
            menu.popUpMenuPositioningItem_atLocation_inView_(None, NSPoint(0, sender.bounds().size.height), sender)
        finally:
            self._menu_open = False
            self.popover.setBehavior_(AK.NSPopoverBehaviorTransient)
        chosen, self._chosen = self._chosen, None
        if chosen is not None:
            self.close()
            try:
                chosen()
            except Exception:
                logging.exception('On Deck action failed')

    @objc.python_method
    def _fill_menu(self, menu, entries):
        for entry in entries:
            if entry is None:
                menu.addItem_(AK.NSMenuItem.separatorItem())
                continue
            title, target = entry[0], entry[1]
            checked = len(entry) > 2 and entry[2]
            item = AK.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, '')
            if isinstance(target, list):
                submenu = AK.NSMenu.alloc().initWithTitle_(title)
                submenu.setAutoenablesItems_(False)
                self._fill_menu(submenu, target)
                item.setSubmenu_(submenu)
            elif target is None:
                item.setEnabled_(False)
            else:
                item.setTarget_(self)
                item.setAction_('menuChoice:')
                item.setTag_(len(self._menu_callbacks))
                self._menu_callbacks.append(target)
            if checked:
                item.setState_(AK.NSControlStateValueOn)
            menu.addItem_(item)

    def menuChoice_(self, item):
        index = item.tag()
        if 0 <= index < len(self._menu_callbacks):
            self._chosen = self._menu_callbacks[index]

    @objc.python_method
    def _act(self, name, *args):
        return lambda: self.actions[name](*args)

    @objc.python_method
    def _song_entries(self, rows):
        return [(f'{title}  ·  {stage}', self._act('deck_pick', key)) for key, title, stage in rows]

    def deckOpen_(self, sender):
        self._dispatch('deck_open')

    def deckPick_(self, sender):
        suggestions = (self._deck or {}).get('suggestions') or []
        if 0 <= sender.tag() < len(suggestions):
            self._dispatch('deck_pick', suggestions[sender.tag()][0])

    def deckStage_(self, sender):
        current = (self._deck or {}).get('stage')
        self._popup(sender, [('Stage', None)] + [
            (label, self._act('deck_stage', stage), stage == current) for stage, label in (self._deck or {}).get('stages', [])])

    def deckNext_(self, sender):
        self._dispatch('deck_next_edit')

    def deckDone_(self, sender):
        self._dispatch('deck_next_done')

    def deckDue_(self, sender):
        self._popup(sender, [('Due date', None), ('In 3 days', self._act('deck_due', 3)),
                             ('In 1 week', self._act('deck_due', 7)), ('In 2 weeks', self._act('deck_due', 14)),
                             ('In 4 weeks', self._act('deck_due', 28)), None,
                             ('Clear due date', self._act('deck_due', 0))])

    def deckChange_(self, sender):
        deck = self._deck or {}
        entries = [('Suggestions', None)] + (self._song_entries(deck.get('more') or []) or [('No suggestions yet', None)])
        if deck.get('all'):
            entries += [None, ('All songs', self._song_entries(deck['all']))]
        entries += [None, ('Snooze for a week, leave On Deck empty', self._act('deck_snooze', 7))]
        self._popup(sender, entries)

    def deckAll_(self, sender):
        self._popup(sender, self._song_entries((self._deck or {}).get('all') or []))

    @objc.python_method
    def _fit_total(self, label, width):
        value = label.stringValue()
        for size in range(22, 11, -1):
            font = _font(size, AK.NSFontWeightSemibold, numeric=True)
            string = AK.NSAttributedString.alloc().initWithString_attributes_(value, {AK.NSFontAttributeName: font})
            if string.size().width <= width - 2 or size == 12:
                label.setFont_(font)
                label.setToolTip_(value)
                break

    def toggle_(self, sender):
        if self.popover.isShown():
            self.close()
            return
        try:
            self.refresh()
            for button in self._focus_buttons:
                button.shows_keyboard_focus = False
                button.setNeedsDisplay_(True)
            AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            button = self.status_item.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, AK.NSMinYEdge)
            window = self.root.window()
            window.makeKeyWindow()
            window.makeFirstResponder_(self._focus_buttons[0] if self._focus_buttons else self.project_button)
        except Exception:
            logging.exception('Could not show menu-bar panel; restoring native menu')
            self.dispose()
            self.status_item.popUpStatusItemMenu_(self.original_menu)

    @objc.python_method
    def close(self):
        self.popover.performClose_(None)

    @objc.python_method
    def _handle_key(self, event):
        if not self.popover.isShown() or self._menu_open:
            return event
        if event.keyCode() == 53:
            self.close()
            return None
        flags = event.modifierFlags() & AK.NSEventModifierFlagDeviceIndependentFlagsMask
        if event.keyCode() == 48 and not flags & (AK.NSEventModifierFlagCommand | AK.NSEventModifierFlagControl | AK.NSEventModifierFlagOption):
            for button in self._focus_buttons:
                button.shows_keyboard_focus = True
                button.setNeedsDisplay_(True)
            window = self.root.window()
            if flags & AK.NSEventModifierFlagShift:
                window.selectPreviousKeyView_(None)
            else:
                window.selectNextKeyView_(None)
            return None
        return event

    @objc.python_method
    def _dispatch(self, name, *args):
        self.close()
        try:
            self.actions[name](*args)
        except Exception:
            logging.exception('Menu-bar action failed: %s', name)

    def openTracker_(self, sender):
        self._dispatch('tracker')

    def openAbleton_(self, sender):
        self._dispatch('ableton')

    def quitApp_(self, sender):
        self._dispatch('quit')

    def showProjects_(self, sender):
        # NSMenu owns submenu navigation and invokes the existing rumps callbacks.
        # Protect the parent popover while that nested native tracking loop runs.
        self._menu_open = True
        self.popover.setBehavior_(AK.NSPopoverBehaviorApplicationDefined)
        try:
            self.project_menu.popUpMenuPositioningItem_atLocation_inView_(None, NSPoint(0, sender.bounds().size.height), sender)
        finally:
            self._menu_open = False
            self.popover.setBehavior_(AK.NSPopoverBehaviorTransient)
            self.close()

    def popoverDidClose_(self, notification):
        self.status_item.button().highlight_(False)

    @objc.python_method
    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self.close()
        if self._key_monitor is not None:
            AK.NSEvent.removeMonitor_(self._key_monitor)
            self._key_monitor = None
        self.status_item.setMenu_(self.original_menu)
        self.status_item.button().setTarget_(self.original_target)
        self.status_item.button().setAction_(self.original_action)
