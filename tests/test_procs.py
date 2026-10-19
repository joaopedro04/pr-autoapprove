import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import procs
import pr_bot


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class PidAliveTest(unittest.TestCase):
    def test_alive_and_dead(self):
        self.assertTrue(procs.pid_alive(os.getpid()))
        self.assertFalse(procs.pid_alive(dead_pid()))

    def test_invalid(self):
        for pid in (None, 0, -1, "123"):
            self.assertFalse(procs.pid_alive(pid), pid)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.lock"

    def test_lock_owner(self):
        self.assertIsNone(procs.lock_owner(self.path))
        self.path.write_text("garbage", encoding="ascii")
        self.assertIsNone(procs.lock_owner(self.path))
        self.path.write_text("4242\n", encoding="ascii")
        self.assertEqual(procs.lock_owner(self.path), 4242)

    def test_lock_of_dead_process_is_taken_over(self):
        owner = dead_pid()
        self.path.write_text(str(owner), encoding="ascii")
        with pr_bot.Lock(self.path) as lock:
            self.assertTrue(lock.held)
            self.assertEqual(lock.stale_owner, owner)
            self.assertEqual(procs.lock_owner(self.path), os.getpid())
        self.assertFalse(self.path.exists())

    def test_fresh_lock_of_live_process_is_respected(self):
        self.path.write_text(str(os.getpid()), encoding="ascii")
        with pr_bot.Lock(self.path) as lock:
            self.assertFalse(lock.held)
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
