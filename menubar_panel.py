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

    def drawRect_(self, rect):
        box = AK.NSInsetRect(self.bounds(), 1, 1)
        if self.hovered or self.isHighlighted():
            background = _dynamic('hover')
            if self.isHighlighted():
                background = background.blendedColorWithFraction_ofColor_(0.08, _dynamic('text'))
            _rounded(box, 6, background)
        foreground = _dynamic('text')
        attrs = {AK.NSFontAttributeName: _font(13, AK.NSFontWeightMedium if self.primary else AK.NSFontWeightRegular),
                 AK.NSForegroundColorAttributeName: foreground}
        title = AK.NSAttributedString.alloc().initWithString_attributes_(self.title(), attrs)
        title.drawAtPoint_((8, (self.bounds().size.height - title.size().height) / 2))
        if self.shortcut:
            hint = AK.NSAttributedString.alloc().initWithString_attributes_(self.shortcut, {
                AK.NSFontAttributeName: _font(12), AK.NSForegroundColorAttributeName: _dynamic('secondary')})
            hint.drawAtPoint_((self.bounds().size.width - hint.size().width - 8,
                               (self.bounds().size.height - hint.size().height) / 2))
        if self.shows_keyboard_focus and self.window() and self.window().firstResponder() == self:
            path = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AK.NSInsetRect(box, 1, 1), 5, 5)
            _dynamic('focus').setStroke()
            path.setLineWidth_(1.5)
            path.stroke()


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
    def _button(self, title, action, shortcut='', primary=False):
        button = PanelButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 42))
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        button.setAccessibilityLabel_(title)
        button.primary = primary
        button.shortcut = shortcut
        self.root.addSubview_(button)
        return button

    @objc.python_method
    def update(self, *, status, today, week, run, show_open_ableton, state):
        display = (status, today, week, run, show_open_ableton, state)
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
        metrics_y = 43 + status_height + 16
        self.root.metrics_y = metrics_y
        for caption, value, x in ((self.today_caption, self.today_label, 16), (self.week_caption, self.week_label, 176)):
            caption.setFrame_(NSMakeRect(x, metrics_y, 128, 18))
            value.setFrame_(NSMakeRect(x, metrics_y + 22, 128, 28))
            self._fit_total(value, 128)
        self.run_caption.setFrame_(NSMakeRect(16, metrics_y + 70, 90, 19))
        self.run_label.setFrame_(NSMakeRect(126, metrics_y + 70, 178, 19))
        self.root.separator_ys = [metrics_y + 103]
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
        buttons = [self.project_button] + ([self.ableton_button] if show_open_ableton else []) + [self.tracker_button, self.quit_button]
        for index, button in enumerate(buttons):
            button.setNextKeyView_(buttons[(index + 1) % len(buttons)])
        self.root.setNeedsDisplay_(True)

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
            for button in (self.project_button, self.ableton_button, self.tracker_button, self.quit_button):
                button.shows_keyboard_focus = False
                button.setNeedsDisplay_(True)
            AK.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            button = self.status_item.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, AK.NSMinYEdge)
            window = self.root.window()
            window.makeKeyWindow()
            window.makeFirstResponder_(self.project_button)
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
            for button in (self.project_button, self.ableton_button, self.tracker_button, self.quit_button):
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
    def _dispatch(self, name):
        self.close()
        try:
            self.actions[name]()
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
