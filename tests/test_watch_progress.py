import unittest

from watch_progress import should_stop


class WatchProgressTests(unittest.TestCase):
    def test_halt_stops_monitor_by_default(self):
        state = {"remaining": 10, "halted": True, "failed": 1}
        self.assertTrue(should_stop(state, False, False))

    def test_keep_running_continues_monitoring_through_halt(self):
        state = {"remaining": 10, "halted": True, "failed": 1}
        self.assertFalse(should_stop(state, False, True))

    def test_monitor_stops_when_complete_or_once_requested(self):
        self.assertTrue(should_stop(
            {"remaining": 0, "halted": True, "failed": 1}, False, True))
        self.assertTrue(should_stop(
            {"remaining": 10, "halted": False, "failed": 0}, True, True))


if __name__ == "__main__":
    unittest.main()
