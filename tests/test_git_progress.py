"""Git clone/fetch progress is live only for interactive terminals."""

import subprocess
import unittest
from unittest.mock import patch

from odoo_devops_tools import odoo_compose


class GitProgress(unittest.TestCase):
    def test_clone_and_fetch_stream_progress_on_tty(self):
        for action in ("clone", "fetch"):
            with self.subTest(action=action):
                result = subprocess.CompletedProcess([], 0, "ok\n", None)
                with patch.object(odoo_compose.sys, "stderr") as stderr, \
                        patch.object(odoo_compose.subprocess, "run", return_value=result) as run:
                    stderr.isatty.return_value = True
                    self.assertEqual(odoo_compose._run(["git", action, "origin"]), "ok")
                args, kwargs = run.call_args
                self.assertEqual(args[0][:5], ["git", "-c", "maintenance.auto=false", action, "--progress"])
                self.assertIsNone(kwargs["stderr"])
                self.assertEqual(kwargs["stdout"], subprocess.PIPE)

    def test_noninteractive_git_is_captured(self):
        result = subprocess.CompletedProcess([], 0, "ok\n", "finished\n")
        with patch.object(odoo_compose.sys, "stderr") as stderr, \
                patch.object(odoo_compose.subprocess, "run", return_value=result) as run:
            stderr.isatty.return_value = False
            self.assertEqual(odoo_compose._run(["git", "fetch", "origin"]), "ok")
        args, kwargs = run.call_args
        self.assertNotIn("--progress", args[0])
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)

    def test_other_git_operations_stay_captured_on_tty(self):
        result = subprocess.CompletedProcess([], 0, "deadbeef\n", "")
        with patch.object(odoo_compose.sys, "stderr") as stderr, \
                patch.object(odoo_compose.subprocess, "run", return_value=result) as run:
            stderr.isatty.return_value = True
            self.assertEqual(odoo_compose._run(["git", "rev-parse", "HEAD"]), "deadbeef")
        args, kwargs = run.call_args
        self.assertNotIn("--progress", args[0])
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
