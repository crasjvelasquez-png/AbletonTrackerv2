"""Native-boundary failure cases defined before detector implementation."""
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

if __name__=='__main__': unittest.main()
