#!/usr/bin/env python3
"""E2E: real NSWindow + floating glass sidebar. Writes report.json + screenshots.

Failure modes covered: rows missing or section headings present, selection not
reaching the callback, programmatic select echoing back, toggle not hiding the
panel, the detail view changing size when the sidebar shows/hides, the toolbar
toggle not reaching the sidebar through the responder chain, pills too small.
Usage: verify_native_sidebar.py [out_dir]
"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import AppKit as AK, Foundation as FN, Quartz
import native_sidebar as ns

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/native_sidebar_verify"); out.mkdir(parents=True, exist_ok=True)
app = AK.NSApplication.sharedApplication(); app.setActivationPolicy_(AK.NSApplicationActivationPolicyRegular)
mask = AK.NSWindowStyleMaskTitled | AK.NSWindowStyleMaskClosable | AK.NSWindowStyleMaskResizable
win = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(FN.NSMakeRect(200, 200, 1000, 640), mask, AK.NSBackingStoreBuffered, False)
detail = AK.NSView.alloc().initWithFrame_(FN.NSMakeRect(0, 0, 600, 400))
detail.setWantsLayer_(True); detail.layer().setBackgroundColor_(AK.NSColor.systemTealColor().CGColor())
picked = []
sb = ns.NativeSidebar(win, detail, picked.append); sb.install(); win.makeKeyAndOrderFront_(None); app.activateIgnoringOtherApps_(True)

def pump(t=0.4):
    end = time.time() + t
    while time.time() < end:
        AK.NSRunLoop.currentRunLoop().runUntilDate_(FN.NSDate.dateWithTimeIntervalSinceNow_(0.05))

def shot(name):
    img = Quartz.CGWindowListCreateImage(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, win.windowNumber(), Quartz.kCGWindowImageBoundsIgnoreFraming)
    if img:
        rep = AK.NSBitmapImageRep.alloc().initWithCGImage_(img)
        (out / name).write_bytes(bytes(rep.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {})))

pump(0.6)
ov = sb.controller.outline
r = {}
titles = [ov.itemAtRow_(i).title for i in range(ov.numberOfRows())]
r["rows"] = titles
r["rows_exact_no_headings"] = titles == ["Dashboard", "Settings", "Planner"]
r["rows_have_icons"] = all(ov.viewAtColumn_row_makeIfNecessary_(0, i, True).imageView().image() is not None for i in range(3))
r["pill_rows_custom"] = isinstance(ov.rowViewAtRow_makeIfNecessary_(0, True), ns.PillRowView)
r["row_height_large"] = ov.rowHeight() >= 38
r["glass_floats_over_detail"] = sb.glass.superview() is sb.container and sb.container.subviews()[-1] is sb.glass
r["initial_selection_silent"] = picked == [] and ov.itemAtRow_(ov.selectedRow()).view_id == "dashboard"
for name in ("Settings", "Planner", "Dashboard"):
    ov.selectRowIndexes_byExtendingSelection_(FN.NSIndexSet.indexSetWithIndex_(titles.index(name)), False); pump(0.1)
r["callback_sequence_ok"] = picked == ["settings", "planner", "dashboard"]
n = len(picked); sb.select("settings"); pump(0.1)
r["programmatic_select_silent"] = len(picked) == n
sb.select("dashboard"); pump(0.2)
shot("open.png")
w_open = detail.frame().size.width
# toolbar toggle path: send toggleSidebar: through the responder chain like the toolbar button does
win.makeFirstResponder_(detail)
r["toggle_reaches_sidebar"] = bool(detail.tryToPerform_with_(b"toggleSidebar:", None)); pump(0.6)
r["collapsed_via_responder_chain"] = sb.collapsed and sb.glass.frame().origin.x < 0
r["detail_same_width_when_collapsed"] = detail.frame().size.width == w_open
shot("collapsed.png")
sb.toggle(); pump(0.6)
r["expanded_after_toggle"] = (not sb.collapsed) and sb.glass.frame().origin.x == ns.MARGIN
r["detail_same_width_when_expanded"] = detail.frame().size.width == w_open
checks = {k: v for k, v in r.items() if isinstance(v, bool)}
r["passed"] = all(checks.values())
(out / "report.json").write_text(json.dumps(r, indent=2))
print(json.dumps(r, indent=2)); sys.exit(0 if r["passed"] else 1)
