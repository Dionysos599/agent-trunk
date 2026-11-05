"""The AgentTrunk name: entry points, environment variables, and the folders that still carry the old name."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cmws_ws import cli
from cmws_ws.kv import MemoryKV
from cmws_ws.repo import Repository
from cmws_ws.sync import IGNORE_FILES, LEGACY_STATE_DIR, STATE_DIR, Checkout, state_dir_of

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SRC = HERE.parent / "src"


def run(args, **kw):
    env = dict(os.environ, PYTHONPATH=str(SRC))
    env.update(kw.pop("env", {}))
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=60, **kw)


class EntryPointTests(unittest.TestCase):
    def test_the_three_ways_to_start_it(self):
        for args in ([str(ROOT / "bin" / "agenttrunk"), "-h"], [sys.executable, "-m", "agenttrunk", "-h"],
                     [sys.executable, "-m", "cmws_ws", "-h"]):
            r = run(args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertIn("usage: agenttrunk", r.stdout, args)
            self.assertNotIn("usage: cmws", r.stdout)

    def test_the_old_module_path_still_works(self):
        r = run([sys.executable, "-m", "cmws_ws.cli", "-h"])
        self.assertIn("usage: agenttrunk", r.stdout)

    def test_help_text_uses_the_new_name_and_lists_the_new_commands(self):
        out = run([sys.executable, "-m", "agenttrunk", "-h"]).stdout
        for word in ("agenttrunk --url", "board", "mcp", "orchestrate", "AGENTTRUNK_URL"):
            self.assertIn(word, out)
        self.assertNotIn("python3 -m cmws_ws.cli", out)

    def test_the_wrapper_is_executable_and_independent_of_the_working_directory(self):
        self.assertTrue(os.access(ROOT / "bin" / "agenttrunk", os.X_OK))
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([str(ROOT / "bin" / "agenttrunk"), "-h"], cwd=tmp, capture_output=True, text=True,
                               env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_mcp_subcommand_is_reachable_and_has_its_own_help(self):
        r = run([sys.executable, "-m", "agenttrunk", "mcp", "-h"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--board", r.stdout)
        self.assertIn("--file-tools", r.stdout)
        r = run([sys.executable, "-m", "agenttrunk", "--url", "http://x:1", "--main", "m", "mcp", "--board", "bad-name"])
        self.assertEqual(r.returncode, 2)                                           # options after `mcp` are really read

    def test_pyproject_exposes_the_command(self):
        text = (HERE.parent / "pyproject.toml").read_text()
        self.assertIn('name = "agenttrunk"', text)
        self.assertIn('agenttrunk = "cmws_ws.cli:main"', text)
        self.assertIn("requires-python", text)
        try:
            import tomllib
        except ImportError:                                                          # Python 3.10
            return
        data = tomllib.loads(text)
        module, func = data["project"]["scripts"]["agenttrunk"].split(":")
        self.assertTrue(callable(getattr(__import__(module, fromlist=[func]), func)))
        self.assertEqual(data["project"]["dependencies"], [])                        # still standard library only


class VersionTests(unittest.TestCase):
    def test_one_version_everywhere(self):
        import re
        from cmws_ws import __version__
        pyproject = (HERE.parent / "pyproject.toml").read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1), __version__)
        self.assertIn(f"## {__version__}", (ROOT / "CHANGELOG.md").read_text())
        from cmws_ws.mcp import server
        self.assertEqual(server.VERSION, __version__)

    def test_the_command_reports_it(self):
        from cmws_ws import __version__
        r = run([sys.executable, "-m", "agenttrunk", "--version"])
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"agenttrunk {__version__}"))

    def test_the_mcp_server_announces_it(self):
        import json
        from cmws_ws import __version__
        r = subprocess.run([sys.executable, "-m", "cmws_ws", "--url", "http://127.0.0.1:1", "mcp", "--agent", "v"],
                           input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n",
                           capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(SRC)), timeout=60)
        self.assertEqual(json.loads(r.stdout)["result"]["serverInfo"], {"name": "agenttrunk", "version": __version__})


class UnreachableGatewayTests(unittest.TestCase):
    def test_the_command_says_what_is_wrong_instead_of_printing_a_traceback(self):
        r = run([sys.executable, "-m", "agenttrunk", "--url", "http://127.0.0.1:1", "log"])
        self.assertEqual(r.returncode, 3)
        self.assertIn("cannot reach the gateway at http://127.0.0.1:1", r.stderr)
        self.assertIn("make run-gateway", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_the_address_is_taken_from_the_environment_too(self):
        r = run([sys.executable, "-m", "agenttrunk", "log"], env={"AGENTTRUNK_URL": "http://127.0.0.1:2"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("http://127.0.0.1:2", r.stderr)

    def test_the_demo_setup_checks_the_gateway_first(self):
        r = run([str(ROOT / "examples" / "mcp-demo" / "setup.sh"), "x"], env={"AGENTTRUNK_URL": "http://127.0.0.1:1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cannot reach the gateway", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("AGENTTRUNK_", "CMWS_"))}
        self.addCleanup(lambda: (self._clean(), os.environ.update(self.saved)))

    @staticmethod
    def _clean():
        for k in list(os.environ):
            if k.startswith(("AGENTTRUNK_", "CMWS_")):
                del os.environ[k]

    def test_new_names_win_and_old_names_still_work(self):
        self.assertEqual(cli._env("URL", "dflt"), "dflt")
        os.environ["CMWS_URL"] = "old"
        self.assertEqual(cli._env("URL", "dflt"), "old")
        os.environ["AGENTTRUNK_URL"] = "new"
        self.assertEqual(cli._env("URL", "dflt"), "new")
        os.environ["AGENTTRUNK_LLM_HOST"] = "h"
        self.assertEqual(cli._env("LLM_HOST"), "h")

    def test_the_url_default_comes_from_the_environment(self):
        os.environ["AGENTTRUNK_URL"] = "http://example:9"
        r = run([sys.executable, "-m", "agenttrunk", "log"], env={"AGENTTRUNK_URL": "http://127.0.0.1:1"})
        self.assertNotEqual(r.returncode, 0)                                         # it tried that address, not 8080
        self.assertIn("127.0.0.1:1", r.stderr + r.stdout)


class CheckoutNameTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository.init(MemoryKV(), {"a.txt": b"1\n"})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "w"

    def test_new_checkouts_use_the_new_folder_name(self):
        Checkout.clone(self.repo, "x", self.root)
        self.assertTrue((self.root / STATE_DIR / "state.json").exists())
        self.assertFalse((self.root / LEGACY_STATE_DIR).exists())
        self.assertEqual(state_dir_of(self.root), ".agenttrunk")

    def test_a_checkout_made_before_the_rename_keeps_working_under_its_old_name(self):
        Checkout.clone(self.repo, "x", self.root)
        (self.root / STATE_DIR).rename(self.root / LEGACY_STATE_DIR)
        self.assertEqual(state_dir_of(self.root), ".cmws")
        co = Checkout.open(self.repo, self.root)
        (self.root / "b.txt").write_text("2\n")
        self.assertEqual(co.status(), {"b.txt": "A"})                                # its own folder is not content
        self.assertEqual(co.push("b").status, "pushed")
        self.assertTrue((self.root / LEGACY_STATE_DIR / "state.json").exists())
        self.assertFalse((self.root / STATE_DIR).exists())
        co.pull()
        self.assertEqual(co.status(), {})

    def test_a_folder_that_is_not_a_checkout_is_still_refused(self):
        from cmws_ws.sync import SyncError
        self.root.mkdir()
        with self.assertRaises(SyncError):
            Checkout.open(self.repo, self.root)
        Checkout.clone(self.repo, "y", Path(self.tmp.name) / "z")
        with self.assertRaises(SyncError):                                           # already a checkout
            Checkout.clone(self.repo, "y", Path(self.tmp.name) / "z")

    def test_both_ignore_files_are_honoured(self):
        self.assertEqual(IGNORE_FILES, (".agenttrunkignore", ".cmwsignore"))
        co = Checkout.clone(self.repo, "x", self.root)
        (self.root / ".agenttrunkignore").write_text("*.log\n")
        (self.root / ".cmwsignore").write_text("*.tmp\n")
        for n in ("a.log", "b.tmp", "c.txt"):
            (self.root / n).write_text("x\n")
        self.assertEqual(sorted(co.status()), ["c.txt"])          # the ignore files themselves are not synced either

    def test_the_bookkeeping_folders_are_never_content(self):
        co = Checkout.clone(self.repo, "x", self.root)
        (self.root / ".cmws").mkdir()
        (self.root / ".cmws" / "junk").write_text("x")
        self.assertEqual(co.status(), {})
        self.assertTrue(co.ignored(".agenttrunk/state.json"))
        self.assertTrue(co.ignored(".cmws/state.json"))


if __name__ == "__main__":
    unittest.main()
