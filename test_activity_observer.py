"""Native-boundary failure cases defined before detector implementation."""
import time
import unittest
from unittest.mock import patch
from activity_observer import InputEvidence, ProbeWorker

class InputEvidenceTests(unittest.TestCase):
    def test_activation_and_pointer_motion_cannot_create_attendance(self):
        e=InputEvidence()
        e.focus(42,100)
        e.event('move',101,42)
        self.assertIsNone(e.last_input)
        e.event('key',102,42)
        self.assertEqual(e.last_input,102)

    def test_other_app_events_do_not_refresh_live(self):
        e=InputEvidence(); e.focus(42,100)
        e.event('key',101,99)
        self.assertIsNone(e.last_input)

    def test_return_to_live_requires_a_new_event(self):
        e=InputEvidence(); e.focus(42,100); e.event('click',101,42)
        e.focus(None,105); e.focus(42,110)
        self.assertIsNone(e.last_input)

class ProbeWorkerTests(unittest.TestCase):
    def test_stale_generation_result_is_discarded(self):
        p=ProbeWorker(lambda pid: True)
        p.invalidate(2)
        p.publish(1,True,100)
        self.assertIsNone(p.result(2,101)[0])
        p.publish(2,True,100)
        self.assertIs(p.result(2,101)[0],True)
        self.assertIsNone(p.result(2,111)[0])
        p.close()

    def _settle(self,p):
        p.future.result(timeout=2)

    def test_success_allows_a_request_every_poll(self):
        # Polls are 5s apart from request to request; measuring from completion
        # skipped every other poll and let evidence age to EVIDENCE_TTL.
        p=ProbeWorker(lambda pid: (time.sleep(0.05),True)[1])
        now=time.monotonic()
        p.request(42,1,now); self._settle(p); first=p.future
        p.request(42,1,now+5)
        self.assertIsNot(p.future,first)
        self._settle(p); p.close()

    def test_new_generation_clears_failure_backoff(self):
        p=ProbeWorker(lambda pid: None)
        now=time.monotonic()
        for i in range(3):
            p.next_at=0; p.request(42,1,now); self._settle(p)
        self.assertGreater(p.next_at,now+10)
        p.invalidate(2)
        before=p.future
        p.request(42,2,now+1)
        self.assertIsNot(p.future,before)
        self._settle(p); p.close()

if __name__=='__main__': unittest.main()
