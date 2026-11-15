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
        card = getattr(self, 'deck_card', None)
        if card is not None:
            _rounded(card, 10, _dynamic('text').colorWithAlphaComponent_(0.045), _dynamic('line'))
        lane_track = getattr(self, 'lane_track', None)
        if lane_track is not None:
            _rounded(lane_track, 8, _dynamic('text').colorWithAlphaComponent_(0.045), _dynamic('line'))
        editing = getattr(self, 'edit_rect', None)
        if editing is not None:
            box = AK.NSInsetRect(editing, 1, 1)
            _rounded(box, 6, _dynamic('background'))
            ring = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AK.NSInsetRect(box, 1, 1), 5, 5)
            _dynamic('focus').setStroke()
            ring.setLineWidth_(1.5)
            ring.stroke()
        if not hasattr(self, 'separator_ys'):
            return
        _dynamic('line').setStroke()
        path = AK.NSBezierPath.bezierPath()
        for y in self.separator_ys:
            path.moveToPoint_((16, y))
            path.lineToPoint_((304, y))
        path.setLineWidth_(0.5)
        path.stroke()

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
            self.align = AK.NSTextAlignmentLeft
            self.multiline = False
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
        style.setAlignment_(self.align)
        style.setLineBreakMode_(AK.NSLineBreakByWordWrapping if self.multiline else AK.NSLineBreakByTruncatingTail)
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
        avail_w = max(0, self.bounds().size.width - 16 - hint_width)
        if self.multiline:
            rect = title.boundingRectWithSize_options_(
                NSMakeSize(avail_w, self.bounds().size.height),
                AK.NSStringDrawingUsesLineFragmentOrigin,
            )
            height = min(self.bounds().size.height, rect.size.height)
        else:
            height = title.size().height
        title.drawInRect_(NSMakeRect(8, (self.bounds().size.height - height) / 2, avail_w, height))

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


class SegmentButton(PanelButton):
    """Segmented lane button; the focused Now lane draws as primary."""

    @objc.python_method
    def paints_hover(self):
        return not self.primary

    @objc.python_method
    def draw_content(self):
        style = AK.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AK.NSTextAlignmentCenter)
        style.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
        weight = AK.NSFontWeightSemibold if self.primary else AK.NSFontWeightMedium
        if self.primary:
            foreground = AK.NSColor.alternateSelectedControlTextColor()
        elif self.hovered or self.isHighlighted():
            foreground = _dynamic('text')
        else:
            foreground = _dynamic('secondary')
        title = AK.NSAttributedString.alloc().initWithString_attributes_(self.title(), {
            AK.NSFontAttributeName: _font(12, weight),
            AK.NSForegroundColorAttributeName: foreground,
            AK.NSParagraphStyleAttributeName: style,
        })
        height = title.size().height
        title.drawInRect_(NSMakeRect(6, (self.bounds().size.height - height) / 2,
                                     max(0, self.bounds().size.width - 12), height))

    def drawRect_(self, rect):
        box = AK.NSInsetRect(self.bounds(), 0.5, 0.5)
        if self.primary:
            _rounded(box, 6, _dynamic('accent'))
        elif self.hovered or self.isHighlighted():
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
        self._show_open_ableton = False
        self._deck = None
        self._menu_callbacks = []
        self._chosen = None
        self._editing = False
        self._edit_original = ''
        self._update_args = None
        self._lane_keys = ['produce', 'mix', 'master']

        self.root = PanelRoot.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 220))
        self.root.panel_controller = self
        glass = AK.NSVisualEffectView.alloc().initWithFrame_(self.root.bounds())
        glass.setMaterial_(AK.NSVisualEffectMaterialHUDWindow)
        glass.setBlendingMode_(AK.NSVisualEffectBlendingModeBehindWindow)
        glass.setState_(AK.NSVisualEffectStateActive)
        glass.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)
        self.root.addSubview_(glass)
        self.root.display_state = 'idle'

        self.deck_caption = self._label('', 12, AK.NSFontWeightSemibold, secondary=True)
        self.deck_title_button = self._button('', 'deckOpen:', 'Open \u203a', primary=True)
        self.deck_title_button.title_font = _font(14, AK.NSFontWeightSemibold)
        self.deck_empty_button = self._button('', 'openTracker:', tone='secondary')
        self.deck_empty_button.multiline = True
        self.deck_check_button = self._button('', 'deckDone:', cls=CheckButton)
        self.deck_next_button = self._button('', 'deckNext:')
        self.deck_next_field = self._field('The one next thing\u2026')

        self.lane_buttons = [self._button('', 'laneFocus:', cls=SegmentButton) for _ in range(3)]
        for index, button in enumerate(self.lane_buttons):
            button.setTag_(index)

        self.stats_label = self._label('', 12, secondary=True)
        self.tracker_button = self._button('Open Tracker', 'openTracker:', primary=True)
        self.more_button = self._button('\u22ef', 'showMore:', tone='secondary')
        self.more_button.align = AK.NSTextAlignmentCenter
        self.more_button.title_font = _font(14, AK.NSFontWeightMedium)
        self.more_button.setToolTip_('More actions')
        self.more_button.setAccessibilityLabel_('More actions')

        self.view_controller = AK.NSViewController.alloc().init()
        self.view_controller.setView_(self.root)
        self.popover = AK.NSPopover.alloc().init()
        self.popover.setContentViewController_(self.view_controller)
        self.popover.setBehavior_(AK.NSPopoverBehaviorTransient)
        self.popover.setAnimates_(False)
        self.popover.setDelegate_(self)

        # Complete all setup before detaching the working fallback menu.
        self._key_monitor = AK.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AK.NSEventMaskKeyDown, self._handle_key)
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
    def _field(self, placeholder):
        field = AK.NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 18))
        field.setBezeled_(False)
        field.setBordered_(False)
        field.setDrawsBackground_(False)
        field.setFocusRingType_(AK.NSFocusRingTypeNone)
        field.setFont_(_font(13))
        field.setTextColor_(_dynamic('text'))
        field.setPlaceholderString_(placeholder)
        field.setLineBreakMode_(AK.NSLineBreakByTruncatingTail)
        field.cell().setUsesSingleLineMode_(True)
        field.cell().setScrollable_(True)
        field.setAccessibilityLabel_('Next task')
        field.setDelegate_(self)
        field.setHidden_(True)
        self.root.addSubview_(field)
        return field

    @objc.python_method
    def _button(self, title, action, shortcut='', primary=False, tone='text', cls=None):
        button = (cls or PanelButton).alloc().initWithFrame_(NSMakeRect(0, 0, 100, 32))
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
    def _current_lane_deck(self):
        deck = self._deck
        if not deck:
            return None
        if 'deck' in deck:
            return deck.get('deck')
        now_lane = deck.get('now_lane')
        for lane in deck.get('lanes') or []:
            if lane.get('key') == now_lane:
                return lane.get('deck')
        return None

    @objc.python_method
    def _lane_label(self, deck):
        if deck.get('now_lane_label'):
            return deck['now_lane_label']
        now_lane = deck.get('now_lane', 'produce')
        for lane in deck.get('lanes') or []:
            if lane.get('key') == now_lane and lane.get('label'):
                return lane['label']
        return str(now_lane).capitalize()

    @objc.python_method
    def update(self, *, status, today, week, run, show_open_ableton, state, deck=None):
        self._update_args = dict(status=status, today=today, week=week, run=run,
                                 show_open_ableton=show_open_ableton, state=state, deck=deck)
        display = (status, today, week, run, show_open_ableton, state, repr(deck), self._editing)
        if display == self._display:
            return
        self._display = display
        self.root.display_state = state
        self._show_open_ableton = bool(show_open_ableton)
        self._deck = deck

        deck_buttons, y = self._layout_deck(deck, 14)
        lane_buttons, y = self._layout_lanes(deck, y)

        today_val = today.removeprefix('Today: ')
        week_val = week.removeprefix('This week: ').removeprefix('Week: ')
        stats_text = f'Today {today_val} \u00b7 Week {week_val}'
        self.stats_label.setStringValue_(stats_text)
        self.stats_label.setAccessibilityLabel_(stats_text)
        self.stats_label.setFrame_(NSMakeRect(16, y, 288, 16))
        y += 26

        self.root.separator_ys = [y]
        y += 10
        self.tracker_button.setFrame_(NSMakeRect(8, y, 266, 32))
        self.more_button.setFrame_(NSMakeRect(278, y, 34, 32))
        height = y + 32 + 12

        self.root.setFrameSize_(NSMakeSize(320, height))
        self.popover.setContentSize_(NSMakeSize(320, height))

        buttons = deck_buttons + lane_buttons + [self.tracker_button, self.more_button]
        self._focus_buttons = buttons
        for index, button in enumerate(buttons):
            button.setNextKeyView_(buttons[(index + 1) % len(buttons)])
        if self._editing:
            self.deck_next_field.setNextKeyView_(lane_buttons[0] if lane_buttons else self.tracker_button)
        self.root.setNeedsDisplay_(True)

    @objc.python_method
    def _layout_deck(self, deck, y):
        """Place the Now-lane deck card; returns visible buttons in keyboard order and next y."""
        for view in (
            self.deck_caption, self.deck_title_button, self.deck_empty_button,
            self.deck_check_button, self.deck_next_button,
        ):
            view.setHidden_(True)
        self.root.deck_card = None
        self.root.edit_rect = None
        lane_deck = self._current_lane_deck()
        editing = self._editing and (lane_deck is not None)
        if not editing:
            # Never hide the field mid-edit: hiding the first responder would end the edit.
            self.deck_next_field.setHidden_(True)
        if not deck:
            return [], y

        lane_label = self._lane_label(deck)
        self.deck_caption.setStringValue_(f'Now \u00b7 {lane_label}')
        self.deck_caption.setAccessibilityLabel_(f'Now: {lane_label}')
        self.deck_caption.setFrame_(NSMakeRect(16, y, 288, 16))
        self.deck_caption.setHidden_(False)
        y += 22

        visible = []

        def show(button, frame):
            button.setFrame_(frame)
            button.setHidden_(False)
            button.setNeedsDisplay_(True)
            visible.append(button)

        card_top = y
        if lane_deck is not None:
            title = lane_deck.get('display_title') or lane_deck.get('title', '')
            full = lane_deck.get('title', title)
            self.deck_title_button.setTitle_(title)
            self.deck_title_button.setToolTip_(full)
            self.deck_title_button.setAccessibilityLabel_(f'Open {full} in Live')
            show(self.deck_title_button, NSMakeRect(12, y + 4, 296, 34))
            y += 40

            task = lane_deck.get('next_task') or lane_deck.get('task') or ''
            if task:
                self.deck_check_button.setTitle_('')
                self.deck_check_button.setToolTip_('Mark done')
                self.deck_check_button.setAccessibilityLabel_(f'Mark done: {task}')
                show(self.deck_check_button, NSMakeRect(12, y, 30, 32))
            nxt = self.deck_next_button
            nxt.setTitle_(task or 'Add the next task\u2026')
            nxt.tone = 'text' if task else 'secondary'
            nxt.shortcut = '' if task else '+'
            nxt.setToolTip_(task or 'Add the next task')
            nxt.setAccessibilityLabel_(f'Edit next task: {task}' if task else 'Add the next task')
            row = NSMakeRect(42, y, 266, 32) if task else NSMakeRect(12, y, 296, 32)
            if editing:
                self.root.edit_rect = row
                self.deck_next_field.setFrame_(NSMakeRect(row.origin.x + 6, y + 7, row.size.width - 14, 18))
                self.deck_next_field.setHidden_(False)
            else:
                show(nxt, row)
            y += 36
        else:
            hint = f'Nothing on deck in {lane_label}. Pick one in Tracker.'
            self.deck_empty_button.setTitle_(hint)
            self.deck_empty_button.setToolTip_(hint)
            self.deck_empty_button.setAccessibilityLabel_(hint)
            show(self.deck_empty_button, NSMakeRect(12, y + 4, 296, 42))
            y += 50

        self.root.deck_card = NSMakeRect(8, card_top, 304, y - card_top)
        return visible, y + 12

    @objc.python_method
    def _layout_lanes(self, deck, y):
        for button in self.lane_buttons:
            button.setHidden_(True)
        self.root.lane_track = None
        if not deck:
            return [], y

        lanes = deck.get('lanes') or [
            {'key': 'produce', 'label': 'Produce'},
            {'key': 'mix', 'label': 'Mix'},
            {'key': 'master', 'label': 'Master'},
        ]
        now_lane = deck.get('now_lane', 'produce')
        self._lane_keys = [lane['key'] for lane in lanes[:3]]
        self.root.lane_track = NSMakeRect(8, y, 304, 30)

        visible = []
        for index, (button, lane) in enumerate(zip(self.lane_buttons, lanes[:3])):
            selected = (lane['key'] == now_lane)
            button.setTitle_(lane['label'])
            button.primary = selected
            button.setToolTip_(f"Switch Now lane to {lane['label']}")
            button.setAccessibilityLabel_(
                f"{lane['label']} lane, selected" if selected else f"{lane['label']} lane"
            )
            button.setFrame_(NSMakeRect(10 + index * 101, y + 2, 98, 26))
            button.setHidden_(False)
            button.setNeedsDisplay_(True)
            visible.append(button)
        return visible, y + 30 + 12

    @objc.python_method
    def _menu_owner(self, submenu):
        supermenu = submenu.supermenu()
        if supermenu is None:
            return None
        for item in supermenu.itemArray():
            if item.submenu() == submenu:
                return item
        return None

    @objc.python_method
    def _more_entries(self):
        entries = []
        if self.project_menu is not None:
            entries.append(('Open Project', self.project_menu))
        if self._show_open_ableton:
            entries.append(('Open Ableton', self._act('ableton')))
        entries.extend([None, ('Quit', self._act('quit'))])
        return entries

    @objc.python_method
    def _popup(self, sender, entries):
        """Native menu under a panel control; runs the chosen callback after it closes."""
        menu = AK.NSMenu.alloc().initWithTitle_('')
        menu.setAutoenablesItems_(False)
        self._menu_callbacks = []
        reattach = []
        self._fill_menu(menu, entries, reattach)
        self._menu_open = True
        self._chosen = None
        picked = False
        self.popover.setBehavior_(AK.NSPopoverBehaviorApplicationDefined)
        try:
            picked = bool(menu.popUpMenuPositioningItem_atLocation_inView_(
                None, NSPoint(0, sender.bounds().size.height), sender))
        finally:
            for item, submenu, owner in reattach:
                item.setSubmenu_(None)
                if owner is not None:
                    owner.setSubmenu_(submenu)
            self._menu_open = False
            self.popover.setBehavior_(AK.NSPopoverBehaviorTransient)
        chosen, self._chosen = self._chosen, None
        if chosen is not None:
            self.close()
            try:
                chosen()
            except Exception:
                logging.exception('Menu-bar popup action failed')
        elif picked:
            self.close()

    @objc.python_method
    def _fill_menu(self, menu, entries, reattach):
        for entry in entries:
            if entry is None:
                menu.addItem_(AK.NSMenuItem.separatorItem())
                continue
            title, target = entry[0], entry[1]
            checked = len(entry) > 2 and entry[2]
            item = AK.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, '')
            if isinstance(target, AK.NSMenu):
                owner = self._menu_owner(target)
                if owner is not None:
                    owner.setSubmenu_(None)
                item.setSubmenu_(target)
                reattach.append((item, target, owner))
            elif isinstance(target, list):
                submenu = AK.NSMenu.alloc().initWithTitle_(title)
                submenu.setAutoenablesItems_(False)
                self._fill_menu(submenu, target, reattach)
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

    def deckOpen_(self, sender):
        self._dispatch('deck_open')

    def laneFocus_(self, sender):
        if self._editing:
            self._finish_edit(True)
        index = sender.tag()
        if 0 <= index < len(self._lane_keys):
            try:
                self.actions['lane_focus'](self._lane_keys[index])
            except Exception:
                logging.exception('Menu-bar action failed: lane_focus')

    def deckNext_(self, sender):
        lane_deck = self._current_lane_deck() or {}
        self._begin_edit(lane_deck.get('next_task') or lane_deck.get('task') or '')

    def deckDone_(self, sender):
        try:
            self.actions['deck_next_complete']()
        except Exception:
            logging.exception('Menu-bar action failed: deck_next_complete')
            return
        self._begin_edit('')

    @objc.python_method
    def _relayout(self):
        if self._update_args is not None:
            self._display = None
            self.update(**self._update_args)

    @objc.python_method
    def _begin_edit(self, text):
        """Turn the next-task row into an in-place field (Reminders-style), keeping the panel open."""
        self._editing = True
        self._edit_original = text
        self.deck_next_field.setStringValue_(text)
        self._relayout()
        if self.deck_next_field.isHidden():
            self._editing = False
            return
        window = self.root.window()
        if window is not None:
            window.makeFirstResponder_(self.deck_next_field)
            editor = self.deck_next_field.currentEditor()
            if editor is not None:
                editor.moveToEndOfDocument_(None)

    @objc.python_method
    def _finish_edit(self, commit):
        if not self._editing:
            return
        self._editing = False
        text = self.deck_next_field.stringValue().strip()
        window = self.root.window()
        if window is not None:
            window.makeFirstResponder_(self.deck_next_button if self.popover.isShown() else None)
        self._relayout()
        if commit and text != self._edit_original:
            try:
                self.actions['deck_next_save'](text)
            except Exception:
                logging.exception('Menu-bar action failed: deck_next_save')
        if self.popover.isShown() and window is not None and not self.deck_next_button.isHidden():
            window.makeFirstResponder_(self.deck_next_button)

    def controlTextDidEndEditing_(self, notification):
        # Clicking elsewhere or tabbing away keeps what was typed, like other macOS inline fields.
        self._finish_edit(True)

    def showMore_(self, sender):
        if self._editing:
            self._finish_edit(True)
        self._popup(sender, self._more_entries())

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
            window.makeFirstResponder_(self._focus_buttons[0] if self._focus_buttons else self.tracker_button)
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
        flags = event.modifierFlags() & AK.NSEventModifierFlagDeviceIndependentFlagsMask
        if self._editing:
            editor = self.deck_next_field.currentEditor()
            if editor is not None and editor.hasMarkedText():
                return event  # Let input methods finish composing first.
            if event.keyCode() == 53:
                self._finish_edit(False)
                return None
            if event.keyCode() in (36, 76):
                self._finish_edit(True)
                return None
            if event.keyCode() != 48:
                return event
        if event.keyCode() == 53:
            self.close()
            return None
        if event.keyCode() == 48 and not flags & (
            AK.NSEventModifierFlagCommand | AK.NSEventModifierFlagControl | AK.NSEventModifierFlagOption
        ):
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

    def popoverWillClose_(self, notification):
        self._finish_edit(True)

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
