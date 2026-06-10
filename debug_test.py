import unittest
from unittest.mock import patch
import tracker

class Test(unittest.TestCase):
    def test(self):
        with patch.object(tracker, "is_ableton_running", return_value=True), \
             patch.object(tracker, "is_audio_active", return_value=None), \
             patch.object(tracker, "get_idle_seconds", return_value=31), \
             patch.object(tracker, "get_project_name", return_value="Real Project"):
            t = tracker.Tracker()
            t._start("Real Project")
            t.poll_once(paused=False)
            print("STATE:", t.last_state)
            print("RESUME_HINT:", t.resume_hint_project)

if __name__ == '__main__':
    unittest.main()
