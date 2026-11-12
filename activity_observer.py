"""Low-overhead macOS observations. No keys, coordinates or audio are stored."""
import ctypes
import ctypes.util
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from tracking_policy import Observation, INPUT_ALLOWANCE, EVIDENCE_TTL

# Play state only matters once hands-off time nears INPUT_ALLOWANCE, so the
# play-state check starts shortly before the hands-off allowance ends.
AUDIO_PROBE_AFTER=INPUT_ALLOWANCE-2*EVIDENCE_TTL


class InputEvidence:
    def __init__(self):
        self.pid=None
        self.last_input=None
        self.boundary=None
        self.generation=0
        self.focused_at=0.0

    def focus(self,pid,at):
        if self.pid!=pid:
            self.pid=pid
            self.focused_at=at
            self.last_input=None
            self.boundary=at
            self.generation+=1

    def event(self,kind,at,pid):
        if (kind in ('key','click','drag','scroll') and pid is not None and pid==self.pid
            and at-self.focused_at>=0.25):
            self.last_input=at


class ProbeWorker:
    """At most one probe; generation tokens reject stale capture responses."""
    def __init__(self,probe,cancellable=False):
        self.probe=probe
        self.cancellable=cancellable
        self.cancel=threading.Event()
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='tracker-evidence')
        self.lock=threading.Lock()
        self.generation=0
        self.value=None
        self.at=None
        self.future=None
        self.closed=False
        self.next_at=0.0
        self.failures=0
        self.started_at=None
        self.error=None
        self.source=None

    def invalidate(self,generation):
        with self.lock:
            self.cancel.set()
            if generation!=self.generation:
                self.failures=0; self.next_at=0.0  # old backoff must not delay a new listen
            self.generation=generation
            self.value=self.at=None

    def publish(self,generation,value,at):
        with self.lock:
            if generation==self.generation and not self.closed:
                if isinstance(value,tuple):
                    value,self.source=value
                self.value,self.at=value,at
                self.failures=self.failures+1 if value is None else 0
                delay=(5,15,30,60)[min(max(self.failures-1,0),3)] if value is None else 5
                # Success is paced from the request: pacing from completion skipped
                # every other 5s poll and let evidence age to EVIDENCE_TTL.
                self.next_at=at+delay if value is None else (self.started_at or at)+delay

    def result(self,generation,now):
        with self.lock:
            if self.generation!=generation or self.at is None or not 0<=now-self.at<EVIDENCE_TTL:
                return None,None
            return self.value,self.at

    def request(self,pid,generation,now):
        with self.lock:
            if self.closed or now<self.next_at or (self.future is not None and not self.future.done()): return
            self.generation=generation
            self.cancel=threading.Event()
            cancel=self.cancel
            self.started_at=now
            def run():
                try:
                    value=self.probe(pid,cancel) if self.cancellable else self.probe(pid)
                    known=value[0] if isinstance(value,tuple) else value
                    if not cancel.is_set():
                        self.error=None if known is not None else 'Live playback/audio unavailable'
                except Exception as e:
                    value=None; self.error=str(e)[:200]
                if not cancel.is_set():
                    self.publish(generation,value,time.monotonic())
            self.future=self.pool.submit(run)

    def close(self):
        with self.lock:
            self.closed=True
            self.cancel.set()
        self.pool.shutdown(wait=False,cancel_futures=True)


class AXReader:
    """Direct AX read, avoiding one AppleScript subprocess every five seconds."""
    def __init__(self):
        self.lib=ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        self.cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.lib.AXIsProcessTrusted.restype=ctypes.c_bool
        self.lib.AXUIElementCreateApplication.argtypes=[ctypes.c_int]
        self.lib.AXUIElementCreateApplication.restype=ctypes.c_void_p
        self.lib.AXUIElementCopyAttributeValue.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
        self.lib.AXUIElementCopyAttributeValue.restype=ctypes.c_int
        self.lib.AXUIElementSetMessagingTimeout.argtypes=[ctypes.c_void_p,ctypes.c_float]
        self.cf.CFStringCreateWithCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype=ctypes.c_void_p
        self.cf.CFStringGetCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_long,ctypes.c_uint32]
        self.cf.CFStringGetCString.restype=ctypes.c_bool
        self.cf.CFArrayGetCount.argtypes=[ctypes.c_void_p]; self.cf.CFArrayGetCount.restype=ctypes.c_long
        self.cf.CFArrayGetValueAtIndex.argtypes=[ctypes.c_void_p,ctypes.c_long]; self.cf.CFArrayGetValueAtIndex.restype=ctypes.c_void_p
        self.cf.CFRelease.argtypes=[ctypes.c_void_p]
        self.attrs={name:self.cf.CFStringCreateWithCString(None,name.encode(),0x08000100) for name in ['AXWindows','AXTitle']}
        self.prompted=False

    def request_trust(self):
        """Show macOS's Accessibility prompt once per launch so a revoked grant is visible, not silent."""
        if self.prompted: return
        self.prompted=True
        try:
            self.lib.AXIsProcessTrustedWithOptions.argtypes=[ctypes.c_void_p]
            self.lib.AXIsProcessTrustedWithOptions.restype=ctypes.c_bool
            self.cf.CFDictionaryCreate.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_void_p),ctypes.c_long,ctypes.c_void_p,ctypes.c_void_p]
            self.cf.CFDictionaryCreate.restype=ctypes.c_void_p
            keys=(ctypes.c_void_p*1)(ctypes.c_void_p.in_dll(self.lib,'kAXTrustedCheckOptionPrompt').value)
            values=(ctypes.c_void_p*1)(ctypes.c_void_p.in_dll(self.cf,'kCFBooleanTrue').value)
            options=self.cf.CFDictionaryCreate(None,keys,values,1,
                ctypes.addressof(ctypes.c_byte.in_dll(self.cf,'kCFTypeDictionaryKeyCallBacks')),
                ctypes.addressof(ctypes.c_byte.in_dll(self.cf,'kCFTypeDictionaryValueCallBacks')))
            if not options: return None
            try: return self.lib.AXIsProcessTrustedWithOptions(options)
            finally: self.cf.CFRelease(options)
        except (AttributeError,ValueError,OSError):
            return None

    def attribute(self,element,name):
        value=ctypes.c_void_p()
        status=self.lib.AXUIElementCopyAttributeValue(element,self.attrs[name],ctypes.byref(value))
        if status: raise RuntimeError(f'Accessibility read failed ({status})')
        return value.value

    def titles(self,pid):
        if not self.lib.AXIsProcessTrusted():
            self.request_trust()
            raise RuntimeError('Accessibility permission is unavailable')
        app=self.lib.AXUIElementCreateApplication(pid)
        windows=None; titles=[]
        try:
            # Live's UI thread is often busy; 0.25s produced frequent -25204 (cannot complete).
            self.lib.AXUIElementSetMessagingTimeout(app,ctypes.c_float(1.0))
            windows=self.attribute(app,'AXWindows')
            if not windows: return []
            for i in range(min(30,self.cf.CFArrayGetCount(windows))):
                window=self.cf.CFArrayGetValueAtIndex(windows,i)
                value=None
                try:
                    value=self.attribute(window,'AXTitle')
                    if value:
                        buf=ctypes.create_string_buffer(4096)
                        if self.cf.CFStringGetCString(value,buf,len(buf),0x08000100): titles.append(buf.value.decode('utf8'))
                except RuntimeError: continue
                finally:
                    if value: self.cf.CFRelease(value)
            return titles
        finally:
            if windows: self.cf.CFRelease(windows)
            if app: self.cf.CFRelease(app)

    def close(self):
        for attr in self.attrs.values(): self.cf.CFRelease(attr)
        self.attrs.clear()


class NativeObserver:
    def __init__(self):
        import AppKit,Quartz
        self.AppKit,self.Quartz=AppKit,Quartz
        self.workspace=AppKit.NSWorkspace.sharedWorkspace()
        self.lock=threading.RLock()
        self.input=InputEvidence()
        self.wake=threading.Event()
        self.tokens=[]
        self.monitor=None
        self.sleeping=False
        self.last_snapshot_at=None
        self.last_project=None
        self.last_project_at=None
        self.project_pid=None
        self.ax=AXReader()
        self.project_error=None
        self.native_events=False
        self.input_source="native_counters"
        self.next_project_at=0.0
        self.project_failures=0
        self._osc_retry_at=0.0
        self._osc_failures=0
        self.probe=ProbeWorker(self._probe,cancellable=True)
        self.started=False

    def start(self):
        """Call on the menu main thread before its AppKit run loop starts."""
        if self.started: return
        self.started=True
        A=self.AppKit; center=self.workspace.notificationCenter()
        names=[A.NSWorkspaceDidActivateApplicationNotification,A.NSWorkspaceDidLaunchApplicationNotification,
               A.NSWorkspaceDidTerminateApplicationNotification,A.NSWorkspaceWillSleepNotification,
               A.NSWorkspaceDidWakeNotification,A.NSWorkspaceSessionDidResignActiveNotification,
               A.NSWorkspaceSessionDidBecomeActiveNotification]
        def changed(note):
            now=time.monotonic()
            name=note.name()
            with self.lock:
                if name in (A.NSWorkspaceWillSleepNotification,A.NSWorkspaceSessionDidResignActiveNotification):
                    self.sleeping=True
                    self.input.focus(None,now)
                elif name in (A.NSWorkspaceDidWakeNotification,A.NSWorkspaceSessionDidBecomeActiveNotification):
                    self.sleeping=False
                    self.input.last_input=None
                    self.input.boundary=now
                    self.input.generation+=1
                self._sync_focus(now)
                self.next_project_at=0.0
            self.wake.set()
        for name in names:
            self.tokens.append(center.addObserverForName_object_queue_usingBlock_(name,None,None,changed))
        # No NSEvent global monitor: registering one stops the status item from
        # receiving clicks. snapshot() uses a single CGEventSource seconds-since-input reading.
        with self.lock: self._sync_focus(time.monotonic())

    def _live_app(self):
        apps=self.AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.ableton.live')
        return apps[0] if len(apps)==1 else None

    def _sync_focus(self,now):
        live=self._live_app(); front=self.workspace.frontmostApplication()
        pid=int(live.processIdentifier()) if live else None
        focused=bool(pid and front and int(front.processIdentifier())==pid and not self.sleeping)
        # Policy v3: input in any app counts while Live is open, so attribute the
        # session's input counters to Live whenever it runs and the Mac is awake.
        self.input.focus(pid if pid and not self.sleeping else None,now)
        return pid,focused

    def _probe(self,pid,cancel=None):
        from tracker import is_ableton_playing_osc
        now=time.monotonic()
        playing=None
        if now>=self._osc_retry_at:
            playing=is_ableton_playing_osc()
            if playing is None:
                self._osc_failures+=1
                self._osc_retry_at=now+(5,15,30,60)[min(self._osc_failures-1,3)]
            else:
                self._osc_failures=0; self._osc_retry_at=now+5
        return playing,'osc_transport'

    def snapshot(self,current_project=None,paused=False):
        now=time.monotonic(); wall=time.time()
        with self.lock:
            pid,focused=self._sync_focus(now)
            session=self.Quartz.CGSessionCopyCurrentDictionary() or {}
            locked=self.sleeping or bool(session.get('CGSSessionScreenIsLocked',False)) or session.get('kCGSessionOnConsoleKey',True) is False
            observable=True
            # Keep a cheap fallback even when a monitor registered successfully.
            # Registration alone does not prove that this process receives events.
            try:
                age=float(self.Quartz.CGEventSourceSecondsSinceLastEventType(0,0xFFFFFFFF))  # combined session state, any input
                event_at=now-age
                if (self.input.pid and math.isfinite(age) and age>=0
                    and event_at>now-5.5 and event_at>self.input.focused_at
                    and (self.input.last_input is None or event_at>self.input.last_input+0.05)):
                    self.input.event('key',event_at,pid)
                    self.input_source='native_counters'
            except Exception:
                # Healthy, delivered callbacks can still supply evidence; otherwise
                # the policy stops after the finite observability grace.
                observable=(self.native_events and self.input_source=='native_events'
                            and self.input.last_input is not None
                            and now-self.input.last_input<EVIDENCE_TTL)
            if locked:
                self.input.focus(None,now)
            generation=self.input.generation
            if self.project_pid!=pid:
                self.last_project=self.last_project_at=None
                self.project_pid=pid; self.next_project_at=0
                self.project_error=None; self.project_failures=0
                self._osc_retry_at=0; self._osc_failures=0
                self.probe.invalidate(generation)
            if pid and not locked and not paused and now>=self.next_project_at:
                from tracker import get_project_name
                # Same parser, direct AX source. No AppleScript fallback permission prompts.
                try:
                    titles=self.ax.titles(pid)
                    project=get_project_name(current_project,titles=titles)
                    if project is not None:
                        self.last_project,self.last_project_at=project,now
                        self.project_error=None; self.project_failures=0
                    else:
                        self.project_error='No unambiguous Live project title'
                        self.project_failures+=1
                except Exception as e:
                    self.project_error=str(e); self.project_failures+=1
                self.next_project_at=now+(5 if not self.project_failures else (5,5,10,15)[min(self.project_failures-1,3)])
            # Playback counts in the background and without a time limit, so probe
            # whenever input has gone quiet, whichever app is in front.
            eligible=(pid and not locked and not paused and (self.input.last_input is None
                      or now-self.input.last_input>=AUDIO_PROBE_AFTER))
            if eligible: self.probe.request(pid,generation,now)
            else: self.probe.invalidate(generation)
            audio,audio_at=self.probe.result(generation,now)
            project=self.last_project if self.last_project_at is not None and now-self.last_project_at<EVIDENCE_TTL else None
            boundary=self.input.boundary
            if not self.started and not self.input.pid and self.last_snapshot_at is not None:
                boundary=self.last_snapshot_at
            self.last_snapshot_at=now
            self.input.boundary=None
            return Observation(wall=wall,mono=now,pid=pid,foreground=focused,project=project,
                project_at=self.last_project_at,input_at=self.input.last_input,observable=observable,
                locked=locked,audio=audio,audio_at=audio_at,
                source=self.input_source,
                audio_source=self.probe.source,audio_started_at=self.probe.started_at,
                audio_finished_at=audio_at,boundary=boundary,
                error=self.project_error or (self.probe.error if eligible else None))

    def close(self):
        self.probe.close()
        if self.monitor is not None:
            self.AppKit.NSEvent.removeMonitor_(self.monitor); self.monitor=None
        for token in self.tokens: self.workspace.notificationCenter().removeObserver_(token)
        self.tokens.clear()
        self.ax.close()
