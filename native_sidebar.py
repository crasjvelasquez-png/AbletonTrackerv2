"""Native macOS floating glass sidebar for the Tracker window.

The sidebar is a rounded glass panel that floats over the detail view. Showing
or hiding it never resizes the detail view (the web dashboard keeps a fixed
left inset), so the dashboard layout stays still. Navigation lives only here;
the detail view never knows about the rows.
"""
from __future__ import annotations

from typing import Callable

import AppKit as AK
import Foundation as FN
import objc

# (view id, label, SF Symbol). Flat list, no section headings.
ITEMS = [
    ("dashboard", "Dashboard", "chart.bar.xaxis"),
    ("settings", "Settings", "gearshape"),
    ("planner", "Planner", "calendar"),
]
DEFAULT_VIEW = "dashboard"
SIDEBAR_WIDTH = 232
MARGIN = 10            # gap between the glass panel and the window edges
TOP_INSET = 52         # rows start below the traffic lights / toolbar
ROW_HEIGHT = 40
PILL_RADIUS = 11
CORNER_RADIUS = 18
ANIM_SECONDS = 0.16
TOOLBAR_ID = "TrackerMainToolbar"
# Web dashboard keeps this left inset whether the sidebar is shown or hidden.
DETAIL_LEFT_INSET = SIDEBAR_WIDTH + MARGIN


class _Row:
    def __init__(self, view_id, title, symbol):
        self.view_id, self.title, self.symbol = view_id, title, symbol
        self.is_group = False  # kept for callers/tests; there are no group rows
        self.children = []


def build_rows():
    return [_Row(v, label, sym) for v, label, sym in ITEMS]


def _reduce_motion():
    try:
        return bool(AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


class PillRowView(AK.NSTableRowView):
    """Selection pill: larger, rounded, with a clear accent border."""

    def drawSelectionInRect_(self, dirty):
        if self.selectionHighlightStyle() == AK.NSTableViewSelectionHighlightStyleNone:
            return
        rect = FN.NSInsetRect(self.bounds(), 8, 2)
        path = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, PILL_RADIUS, PILL_RADIUS)
        accent = AK.NSColor.controlAccentColor()
        accent.colorWithAlphaComponent_(0.24).setFill()
        path.fill()
        accent.colorWithAlphaComponent_(0.85).setStroke()
        path.setLineWidth_(1.5)
        path.stroke()

    def isEmphasized(self):
        return False  # keep our own pill colors even when the list has focus


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
        return len(self.rows) if item is None else 0

    def outlineView_child_ofItem_(self, ov, index, item):
        return self.rows[index]

    def outlineView_isItemExpandable_(self, ov, item):
        return False

    # --- delegate ----------------------------------------------------
    def outlineView_isGroupItem_(self, ov, item):
        return False

    def outlineView_shouldSelectItem_(self, ov, item):
        return True

    def outlineView_rowViewForItem_(self, ov, item):
        return PillRowView.alloc().init()

    def outlineView_viewForTableColumn_item_(self, ov, column, item):
        cell = ov.makeViewWithIdentifier_owner_("SidebarCell", self)
        if cell is None:
            cell = AK.NSTableCellView.alloc().initWithFrame_(FN.NSMakeRect(0, 0, 200, ROW_HEIGHT))
            cell.setIdentifier_("SidebarCell")
            text = AK.NSTextField.labelWithString_("")
            text.setTranslatesAutoresizingMaskIntoConstraints_(False)
            image = AK.NSImageView.alloc().init()
            image.setTranslatesAutoresizingMaskIntoConstraints_(False)
            cell.addSubview_(image)
            cell.addSubview_(text)
            cell.setTextField_(text)
            cell.setImageView_(image)
            for c in (
                image.leadingAnchor().constraintEqualToAnchor_constant_(cell.leadingAnchor(), 20),
                image.widthAnchor().constraintEqualToConstant_(22),
                image.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
                text.leadingAnchor().constraintEqualToAnchor_constant_(image.trailingAnchor(), 10),
                text.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(cell.trailingAnchor(), -12),
                text.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
            ):
                c.setActive_(True)
        cell.textField().setStringValue_(item.title)
        cell.textField().setFont_(AK.NSFont.systemFontOfSize_weight_(14, AK.NSFontWeightMedium))
        cell.textField().setTextColor_(AK.NSColor.labelColor())
        img = AK.NSImage.imageWithSystemSymbolName_accessibilityDescription_(item.symbol, item.title)
        if img is not None:
            cfg = AK.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
                16, AK.NSFontWeightMedium, AK.NSImageSymbolScaleMedium)
            img = img.imageWithSymbolConfiguration_(cfg)
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
        if item is not None:
            self.on_select(item.view_id)

    # --- programmatic selection (no callback) ------------------------
    @objc.python_method
    def select(self, view_id, notify=False):
        self._silent = not notify
        try:
            for item in self.rows:
                if item.view_id == view_id:
                    row = self.outline.rowForItem_(item)
                    self.outline.selectRowIndexes_byExtendingSelection_(
                        FN.NSIndexSet.indexSetWithIndex_(row), False)
                    return True
            return False
        finally:
            self._silent = False


class GlassContainerView(AK.NSView):
    """Window content: detail view underneath, glass sidebar on top.

    Sits in the responder chain above the web view, so the toolbar's system
    "Toggle Sidebar" button and View > Show/Hide Sidebar reach toggleSidebar_.
    """

    def toggleSidebar_(self, sender):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.toggle()

    def validateMenuItem_(self, item):
        owner = getattr(self, "owner", None)
        if item.action() == b"toggleSidebar:" and owner is not None:
            item.setTitle_("Show Sidebar" if owner.collapsed else "Hide Sidebar")
        return True


class ToolbarDelegate(AK.NSObject):
    """Toolbar holding only the system sidebar toggle."""

    def _ids(self):
        return [AK.NSToolbarToggleSidebarItemIdentifier, AK.NSToolbarFlexibleSpaceItemIdentifier]

    def toolbarDefaultItemIdentifiers_(self, toolbar):
        return self._ids()

    def toolbarAllowedItemIdentifiers_(self, toolbar):
        return self._ids()

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, toolbar, ident, flag):
        return None  # system items only


def _make_glass(frame):
    """Real Liquid Glass on macOS 26+, frosted vibrancy fallback elsewhere."""
    glass_cls = getattr(AK, "NSGlassEffectView", None)
    if glass_cls is not None:
        glass = glass_cls.alloc().initWithFrame_(frame)
        if hasattr(glass, "setCornerRadius_"):
            glass.setCornerRadius_(CORNER_RADIUS)
        return glass, True
    glass = AK.NSVisualEffectView.alloc().initWithFrame_(frame)
    glass.setMaterial_(AK.NSVisualEffectMaterialHUDWindow)
    glass.setBlendingMode_(AK.NSVisualEffectBlendingModeWithinWindow)  # blur the dashboard behind
    glass.setState_(AK.NSVisualEffectStateActive)
    glass.setWantsLayer_(True)
    layer = glass.layer()
    layer.setCornerRadius_(CORNER_RADIUS)
    layer.setMasksToBounds_(True)
    layer.setBorderWidth_(1.0)
    layer.setBorderColor_(AK.NSColor.colorWithWhite_alpha_(1.0, 0.14).CGColor())
    glass.setMaskImage_(_rounded_mask(CORNER_RADIUS))  # clip the blur itself, not just the border
    return glass, False


def _rounded_mask(radius):
    size = radius * 2 + 1
    def _draw(rect):
        AK.NSColor.blackColor().setFill()
        AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()
        return True
    img = AK.NSImage.imageWithSize_flipped_drawingHandler_(FN.NSMakeSize(size, size), False, _draw)
    img.setCapInsets_(AK.NSEdgeInsetsMake(radius, radius, radius, radius))
    img.setResizingMode_(AK.NSImageResizingModeStretch)
    return img


class NativeSidebar:
    """Floating glass sidebar; install() makes it the window's content with the detail view below."""

    def __init__(self, window, detail_view, on_select: Callable[[str], None]):
        self.window = window
        self.detail_view = detail_view
        self.controller = SidebarController.alloc().initWithSelect_(on_select)
        self._collapsed = False

        outline = AK.NSOutlineView.alloc().init()
        outline.setHeaderView_(None)
        outline.setBackgroundColor_(AK.NSColor.clearColor())
        outline.setSelectionHighlightStyle_(AK.NSTableViewSelectionHighlightStyleRegular)
        if hasattr(outline, "setStyle_"):
            outline.setStyle_(AK.NSTableViewStylePlain)
        outline.setIndentationPerLevel_(0)
        outline.setRowSizeStyle_(AK.NSTableViewRowSizeStyleCustom)
        outline.setRowHeight_(ROW_HEIGHT)
        outline.setIntercellSpacing_(FN.NSMakeSize(0, 4))
        column = AK.NSTableColumn.alloc().initWithIdentifier_("main")
        outline.addTableColumn_(column)
        outline.setOutlineTableColumn_(column)
        outline.setDataSource_(self.controller)
        outline.setDelegate_(self.controller)
        self.controller.outline = outline

        self.scroll = AK.NSScrollView.alloc().init()
        self.scroll.setDocumentView_(outline)
        self.scroll.setDrawsBackground_(False)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setAutomaticallyAdjustsContentInsets_(False)
        self.scroll.setContentInsets_(AK.NSEdgeInsetsMake(TOP_INSET - MARGIN, 0, 8, 0))

        self.glass, self.real_glass = _make_glass(FN.NSMakeRect(0, 0, SIDEBAR_WIDTH, 400))
        if self.real_glass and hasattr(self.glass, "setContentView_"):
            self.glass.setContentView_(self.scroll)
        else:
            self.scroll.setFrame_(self.glass.bounds())
            self.scroll.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)
            self.glass.addSubview_(self.scroll)
        self.glass.setShadow_(self._shadow())

        self.container = GlassContainerView.alloc().initWithFrame_(FN.NSMakeRect(0, 0, 900, 600))
        self.container.owner = self

    @staticmethod
    def _shadow():
        shadow = AK.NSShadow.alloc().init()
        shadow.setShadowBlurRadius_(18)
        shadow.setShadowOffset_(FN.NSMakeSize(0, -4))
        shadow.setShadowColor_(AK.NSColor.colorWithWhite_alpha_(0, 0.28))
        return shadow

    def _panel_frame(self, shown=True):
        h = self.container.bounds().size.height
        x = MARGIN if shown else -(SIDEBAR_WIDTH + MARGIN * 2)
        return FN.NSMakeRect(x, MARGIN, SIDEBAR_WIDTH, max(h - MARGIN * 2, 0))

    def install(self, initial=DEFAULT_VIEW):
        frame = self.window.frame()
        self.window.setStyleMask_(self.window.styleMask() | AK.NSWindowStyleMaskFullSizeContentView)
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AK.NSWindowTitleHidden)
        content = self.window.contentRectForFrameRect_(frame)
        self.container.setFrame_(FN.NSMakeRect(0, 0, content.size.width, content.size.height))
        self.container.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)

        # Detail view fills the window and never changes size when the sidebar moves.
        self.detail_view.setFrame_(self.container.bounds())
        self.detail_view.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)
        self.container.addSubview_(self.detail_view)

        self.glass.setFrame_(self._panel_frame(True))
        self.glass.setAutoresizingMask_(AK.NSViewHeightSizable | AK.NSViewMaxXMargin)
        self.container.addSubview_positioned_relativeTo_(self.glass, AK.NSWindowAbove, self.detail_view)

        self.window.setContentView_(self.container)
        self.window.setFrame_display_(frame, True)

        self.toolbar_delegate = ToolbarDelegate.alloc().init()
        toolbar = AK.NSToolbar.alloc().initWithIdentifier_(TOOLBAR_ID)
        toolbar.setDelegate_(self.toolbar_delegate)
        toolbar.setDisplayMode_(AK.NSToolbarDisplayModeIconOnly)
        toolbar.setAllowsUserCustomization_(False)
        toolbar.setShowsBaselineSeparator_(False) if hasattr(toolbar, "setShowsBaselineSeparator_") else None
        self.window.setToolbar_(toolbar)
        if hasattr(self.window, "setToolbarStyle_"):
            self.window.setToolbarStyle_(AK.NSWindowToolbarStyleUnified)

        self.controller.outline.reloadData()
        self.controller.select(initial)

    def select(self, view_id):
        return self.controller.select(view_id)

    def set_collapsed(self, collapsed, animate=True):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        target = self._panel_frame(not collapsed)
        if animate and not _reduce_motion():
            def _anim(ctx):
                ctx.setDuration_(ANIM_SECONDS)
                ctx.setAllowsImplicitAnimation_(True)
                self.glass.animator().setFrame_(target)
                self.glass.animator().setAlphaValue_(0.0 if collapsed else 1.0)
            AK.NSAnimationContext.runAnimationGroup_completionHandler_(_anim, None)
        else:
            self.glass.setFrame_(target)
            self.glass.setAlphaValue_(0.0 if collapsed else 1.0)

    def toggle(self):
        self.set_collapsed(not self._collapsed)

    @property
    def collapsed(self):
        return self._collapsed
