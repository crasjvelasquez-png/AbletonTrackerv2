"""Native macOS source-list sidebar (NSSplitViewController) for the Tracker window.

The sidebar owns navigation only. The detail column hosts whatever view it is
given (the pywebview WKWebView in production) and never knows about the rows.
"""
from __future__ import annotations

from typing import Callable

import AppKit as AK
import Foundation as FN
import objc

# (section title, [(view id, label, SF Symbol)]). Section headers are group rows.
SECTIONS = [
    ("Tracker", [("dashboard", "Dashboard", "chart.bar.xaxis"),
                 ("settings", "Settings", "gearshape")]),
    ("Planner", [("planner", "Overview", "calendar")]),
]
DEFAULT_VIEW = "dashboard"
SIDEBAR_MIN, SIDEBAR_MAX, SIDEBAR_IDEAL = 170, 280, 200


class _Row:
    def __init__(self, view_id, title, symbol=None, children=None):
        self.view_id, self.title, self.symbol = view_id, title, symbol
        self.children = children or []

    @property
    def is_group(self):
        return self.view_id is None


def build_rows():
    return [
        _Row(None, title.upper(), children=[_Row(v, label, sym) for v, label, sym in items])
        for title, items in SECTIONS
    ]


class SidebarController(AK.NSObject):
    """Outline-view data source + delegate. Calls on_select(view_id) on user selection."""

    def initWithSelect_(self, on_select):
        self = objc.super(SidebarController, self).init()
        if self is None:
            return None
        self.on_select = on_select
        self.rows = build_rows()
        self.outline = None
        self._silent = False
        return self

    # --- data source -------------------------------------------------
    def outlineView_numberOfChildrenOfItem_(self, ov, item):
        return len(self.rows if item is None else item.children)

    def outlineView_child_ofItem_(self, ov, index, item):
        return (self.rows if item is None else item.children)[index]

    def outlineView_isItemExpandable_(self, ov, item):
        return bool(item.children)

    # --- delegate ----------------------------------------------------
    def outlineView_isGroupItem_(self, ov, item):
        return item.is_group

    def outlineView_shouldSelectItem_(self, ov, item):
        return not item.is_group

    def outlineView_shouldCollapseItem_(self, ov, item):
        return False  # section headers stay open; the whole column collapses instead

    def outlineView_viewForTableColumn_item_(self, ov, column, item):
        ident = "SidebarHeader" if item.is_group else "SidebarCell"
        cell = ov.makeViewWithIdentifier_owner_(ident, self)
        if cell is None:
            cell = AK.NSTableCellView.alloc().initWithFrame_(FN.NSMakeRect(0, 0, 180, 24))
            cell.setIdentifier_(ident)
            text = AK.NSTextField.labelWithString_("")
            text.setTranslatesAutoresizingMaskIntoConstraints_(False)
            cell.addSubview_(text)
            cell.setTextField_(text)
            if not item.is_group:
                image = AK.NSImageView.alloc().init()
                image.setTranslatesAutoresizingMaskIntoConstraints_(False)
                cell.addSubview_(image)
                cell.setImageView_(image)
                leading = image.trailingAnchor().constraintEqualToAnchor_constant_(
                    text.leadingAnchor(), -6)
                image.leadingAnchor().constraintEqualToAnchor_constant_(
                    cell.leadingAnchor(), 2).setActive_(True)
                image.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()).setActive_(True)
                leading.setActive_(True)
            else:
                text.leadingAnchor().constraintEqualToAnchor_constant_(
                    cell.leadingAnchor(), 2).setActive_(True)
            text.trailingAnchor().constraintLessThanOrEqualToAnchor_(
                cell.trailingAnchor()).setActive_(True)
            text.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()).setActive_(True)
        cell.textField().setStringValue_(item.title)
        if item.is_group:
            cell.textField().setFont_(AK.NSFont.systemFontOfSize_weight_(11, AK.NSFontWeightSemibold))
            cell.textField().setTextColor_(AK.NSColor.secondaryLabelColor())
        else:
            cell.textField().setFont_(AK.NSFont.systemFontOfSize_(13))
            img = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_(item.symbol, item.title)
            cell.imageView().setImage_(img)
            cell.imageView().setContentTintColor_(AK.NSColor.controlAccentColor())
        return cell

    def outlineViewSelectionDidChange_(self, note):
        if self._silent:
            return
        row = self.outline.selectedRow()
        if row < 0:
            return
        item = self.outline.itemAtRow_(row)
        if item is not None and not item.is_group:
            self.on_select(item.view_id)

    # --- programmatic selection (no callback) ------------------------
    @objc.python_method
    def select(self, view_id, notify=False):
        self._silent = not notify
        try:
            for group in self.rows:
                for item in group.children:
                    if item.view_id == view_id:
                        row = self.outline.rowForItem_(item)
                        self.outline.selectRowIndexes_byExtendingSelection_(
                            FN.NSIndexSet.indexSetWithIndex_(row), False)
                        return True
            return False
        finally:
            self._silent = False


class NativeSidebar:
    """Owns the NSSplitViewController; install() swaps it in as the window's content."""

    def __init__(self, window, detail_view, on_select: Callable[[str], None]):
        self.window = window
        self.controller = SidebarController.alloc().initWithSelect_(on_select)

        outline = AK.NSOutlineView.alloc().init()
        outline.setHeaderView_(None)
        outline.setSelectionHighlightStyle_(AK.NSTableViewSelectionHighlightStyleSourceList)
        outline.setFloatsGroupRows_(False)
        outline.setIndentationPerLevel_(0)
        outline.setRowSizeStyle_(AK.NSTableViewRowSizeStyleDefault)
        column = AK.NSTableColumn.alloc().initWithIdentifier_("main")
        outline.addTableColumn_(column)
        outline.setOutlineTableColumn_(column)
        outline.setDataSource_(self.controller)
        outline.setDelegate_(self.controller)
        self.controller.outline = outline

        scroll = AK.NSScrollView.alloc().init()
        scroll.setDocumentView_(outline)
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)  # let the sidebar vibrancy show through
        scroll.setAutohidesScrollers_(True)

        sidebar_vc = AK.NSViewController.alloc().init()
        sidebar_vc.setView_(scroll)
        detail_vc = AK.NSViewController.alloc().init()
        detail_vc.setView_(detail_view)

        self.split = AK.NSSplitViewController.alloc().init()
        self.sidebar_item = AK.NSSplitViewItem.sidebarWithViewController_(sidebar_vc)
        self.sidebar_item.setMinimumThickness_(SIDEBAR_MIN)
        self.sidebar_item.setMaximumThickness_(SIDEBAR_MAX)
        self.sidebar_item.setCanCollapse_(True)
        self.detail_item = AK.NSSplitViewItem.contentListWithViewController_(detail_vc)
        self.split.addSplitViewItem_(self.sidebar_item)
        self.split.addSplitViewItem_(self.detail_item)
        self.split.splitView().setAutosaveName_("TrackerMainSplit")

    def install(self, initial=DEFAULT_VIEW):
        frame = self.window.frame()
        content = self.window.contentRectForFrameRect_(frame).size
        self.split.view().setFrameSize_(content)
        self.split.setPreferredContentSize_(content)
        self.window.setContentViewController_(self.split)
        self.window.setFrame_display_(frame, True)  # the controller swap can shrink the window
        self.window.setTitlebarAppearsTransparent_(False)
        self.controller.outline.expandItem_expandChildren_(None, True)
        self.controller.select(initial)

    def select(self, view_id):
        return self.controller.select(view_id)

    def toggle(self):
        self.split.toggleSidebar_(None)

    @property
    def collapsed(self):
        return bool(self.sidebar_item.isCollapsed())
