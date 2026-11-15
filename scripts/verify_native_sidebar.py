#!/usr/bin/env python3
"""E2E: real NSWindow + NSSplitViewController sidebar. Writes report.json + screenshot.png.

Failure modes covered: window not split-view based, rows missing, group headers
selectable, selection not reaching the callback, programmatic select echoing back,
collapse not working, detail view swallowed by the sidebar.
Usage: verify_native_sidebar.py [out_dir]
"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import AppKit as AK, Foundation as FN, Quartz
from native_sidebar import NativeSidebar

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/native_sidebar_verify"); out.mkdir(parents=True, exist_ok=True)
app = AK.NSApplication.sharedApplication(); app.setActivationPolicy_(AK.NSApplicationActivationPolicyRegular)
mask = AK.NSWindowStyleMaskTitled | AK.NSWindowStyleMaskClosable | AK.NSWindowStyleMaskResizable
win = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(FN.NSMakeRect(200, 200, 900, 600), mask, AK.NSBackingStoreBuffered, False)
detail = AK.NSView.alloc().initWithFrame_(FN.NSMakeRect(0, 0, 600, 400))  # plain view, like the WKWebView
lbl = AK.NSTextField.labelWithString_("DETAIL"); lbl.setFont_(AK.NSFont.systemFontOfSize_(40)); lbl.setFrameOrigin_(FN.NSMakePoint(20, 300)); detail.addSubview_(lbl)
picked = []
sb = NativeSidebar(win, detail, picked.append); sb.install(); win.makeKeyAndOrderFront_(None); app.activateIgnoringOtherApps_(True)

def pump(t=0.4):
    end = time.time() + t
    while time.time() < end:
        AK.NSRunLoop.currentRunLoop().runUntilDate_(FN.NSDate.dateWithTimeIntervalSinceNow_(0.05))

pump(0.6)
ov = sb.controller.outline
r = {}
r["window_uses_split_vc"] = isinstance(win.contentViewController(), AK.NSSplitViewController)
r["sidebar_behavior_is_sidebar"] = sb.sidebar_item.behavior() == AK.NSSplitViewItemBehaviorSidebar
r["highlight_style_source_list"] = ov.selectionHighlightStyle() == AK.NSTableViewSelectionHighlightStyleSourceList
titles = [ov.itemAtRow_(i).title for i in range(ov.numberOfRows())]
r["rows"] = titles
r["has_dashboard_settings_planner"] = all(t in titles for t in ("Dashboard", "Settings", "PLANNER", "Overview"))
r["initial_selection_silent"] = picked == [] and ov.itemAtRow_(ov.selectedRow()).view_id == "dashboard"
# click-equivalent selection of each row
for name in ("Settings", "Overview", "Dashboard"):
    ov.selectRowIndexes_byExtendingSelection_(FN.NSIndexSet.indexSetWithIndex_(titles.index(name)), False); pump(0.1)
r["callback_sequence"] = picked
# group header must refuse selection
hdr = titles.index("PLANNER")
r["group_header_not_selectable"] = not sb.controller.outlineView_shouldSelectItem_(ov, ov.itemAtRow_(hdr))
n = len(picked); sb.select("settings"); pump(0.1)
r["programmatic_select_silent"] = len(picked) == n and ov.itemAtRow_(ov.selectedRow()).view_id == "settings"
w_open = sb.split.splitView().subviews()[0].frame().size.width
sb.toggle(); pump(0.8); r["collapsed_after_toggle"] = sb.collapsed
r["detail_fills_when_collapsed"] = detail.frame().size.width > 800
sb.toggle(); pump(0.8); r["expanded_after_second_toggle"] = not sb.collapsed
r["sidebar_width_open"] = w_open
sb.select("dashboard"); pump(0.3); r["window_size"] = [win.frame().size.width, win.frame().size.height]
wid = win.windowNumber()
img = Quartz.CGWindowListCreateImage(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, wid, Quartz.kCGWindowImageBoundsIgnoreFraming)
if img:
    rep = AK.NSBitmapImageRep.alloc().initWithCGImage_(img)
    (out / "screenshot.png").write_bytes(bytes(rep.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {})))
checks = {k: v for k, v in r.items() if isinstance(v, bool)}
r["callback_sequence_ok"] = picked == ["settings", "planner", "dashboard"]
checks["callback_sequence_ok"] = r["callback_sequence_ok"]
r["passed"] = all(checks.values())
(out / "report.json").write_text(json.dumps(r, indent=2))
print(json.dumps(r, indent=2)); sys.exit(0 if r["passed"] else 1)
