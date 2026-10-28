#!/usr/bin/env python3
"""Verify the native input boundary with a harmless one-pixel test scroll.

Uses the foreground app as a stand-in for Live and never constructs Tracker or
opens its database. Records event type/timestamps only. Keeps the user's app
and document open. Output: /tmp/tracker-native-input-check.json.
"""
import json,threading,time
from pathlib import Path
import AppKit,Quartz
from Foundation import NSRunLoop,NSDate,NSTimer
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from activity_observer import NativeObserver
app=AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
n=NativeObserver()
front=n.workspace.frontmostApplication()
name=str(front.localizedName())
# The real foreground app is used as a harmless stand-in for Live identity.
# This process never constructs a tracker or opens the user's database.
n._live_app=lambda: front
n.ax.titles=lambda pid:['Native input verification fixture']
n.start()
initial=n.snapshot()
def post():
 time.sleep(0.5)
 e=Quartz.CGEventCreateScrollWheelEvent(Quartz.CGEventSourceCreate(0),Quartz.kCGScrollEventUnitPixel,1,1)
 Quartz.CGEventPost(Quartz.kCGHIDEventTap,e)
threading.Thread(target=post,daemon=True).start()
end=time.monotonic()+2
while time.monotonic()<end:
 NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
final=n.snapshot()
result={'foreground_app':name,'native_monitor':n.native_events,'before_input':initial.input_at,
        'after_input':final.input_at,'initial_foreground':initial.foreground,'final_foreground':final.foreground,'source':final.source,'received_qualifying_os_event':final.input_at is not None,
        'scope':'Real OS event delivery; foreground identity and project title are fixture adapters; no DB writes.'}
n.close()
Path('/tmp/tracker-native-input-check.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
assert result['received_qualifying_os_event']
