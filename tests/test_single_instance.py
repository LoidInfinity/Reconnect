"""Single-instance guarantee: only one guard may touch the network at a time.

The lock is a Windows named mutex, so it is released by the kernel when the
owning process dies instead of going stale like a PID file.
"""
import argparse
import logging
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
import uuid
from unittest.mock import patch

import gui
import main
import repairs

# tests/ 上一级是仓库根，源码位于 src/
SRC_DIR = Path(__file__).resolve().parent.parent / "src"


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.single_instance")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False
        self.addCleanup(repairs.release_single_instance)

    def _name(self, label: str) -> str:
        return f"Test-{label}-{uuid.uuid4().hex[:8]}"

    def test_same_name_is_idempotent_within_a_process(self):
        # main() 预检查与 monitor_loop 会各取一次，同进程重复获取必须成功，
        # 且不能重复创建句柄（否则会泄漏）。
        name = self._name("idem")
        self.assertTrue(repairs.acquire_single_instance(name))
        handles_before = len(repairs._single_instance_handles)
        self.assertTrue(repairs.acquire_single_instance(name))
        self.assertEqual(len(repairs._single_instance_handles), handles_before)

    def test_release_allows_reacquire(self):
        name = self._name("release")
        self.assertTrue(repairs.acquire_single_instance(name))
        repairs.release_single_instance()
        self.assertTrue(repairs.acquire_single_instance(name))

    def test_different_names_do_not_collide(self):
        self.assertTrue(repairs.acquire_single_instance(self._name("a")))
        self.assertTrue(repairs.acquire_single_instance(self._name("b")))

    @unittest.skipUnless(sys.platform == "win32", "named mutex is Windows-only")
    def test_lock_is_exclusive_across_processes(self):
        name = self._name("xproc")
        script = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, r"{SRC_DIR}")
            import repairs
            print("ACQUIRED" if repairs.acquire_single_instance({name!r}) else "BUSY", flush=True)
            time.sleep(20)
            """
        )
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = proc.stdout.readline().strip()
            self.assertEqual(first, "ACQUIRED", "子进程应当拿到锁")
            # Another process already owns it: we must be refused.
            self.assertFalse(repairs.acquire_single_instance(name))
        finally:
            proc.kill()
            proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_cli_monitor_loop_exits_when_locked(self):
        # monitor_loop 取锁失败时必须直接退出，不能再写 PID 或检测网络。
        with patch.object(repairs, "acquire_single_instance", return_value=False):
            with self.assertRaises(SystemExit) as caught:
                main.monitor_loop(argparse.Namespace(), self.logger)
        self.assertEqual(caught.exception.code, 2)

    def test_gui_worker_reports_busy_instead_of_monitoring(self):
        events = []
        with patch.object(repairs, "acquire_single_instance", return_value=False), \
             patch.object(gui, "emit", side_effect=lambda kind, **data: events.append(dict(kind=kind, **data))), \
             patch.object(main, "is_internet_reachable") as probe:
            gui.worker(Path("unused.json"))
        probe.assert_not_called()
        self.assertTrue(any(item["kind"] == "log" for item in events))
        self.assertTrue(any(item.get("tone") == "error" for item in events))


if __name__ == "__main__":
    unittest.main()
