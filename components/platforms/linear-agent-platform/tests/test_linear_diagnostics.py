import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import linear_diagnostics as diag


def make_home(tmp: Path, *, with_linear: bool = True) -> Path:
    bridge, mcp = tmp / "bridge.sqlite3", tmp / "mcp.sqlite3"
    con = sqlite3.connect(bridge)
    con.executescript(
        "CREATE TABLE outbox (id TEXT, operation TEXT, state TEXT, attempts INT, updated_at INT);"
        "CREATE TABLE issue_session_bindings (issue_id TEXT PRIMARY KEY, session_id TEXT);"
        "INSERT INTO outbox VALUES ('a','activity','delivered',1,1),('b','activity','dead',5,2);"
        "INSERT INTO issue_session_bindings VALUES ('issue-1','sess-1');"
    )
    con.commit(); con.close()
    con = sqlite3.connect(mcp)
    con.executescript(
        "CREATE TABLE linear_mcp_operations (operation_key TEXT, tool_name TEXT, status TEXT, error_code TEXT, updated_at INT);"
        "INSERT INTO linear_mcp_operations VALUES ('0123456789abcdefSECRET','linear_save_issue','outcome_unknown','timeout',3);"
    )
    con.commit(); con.close()
    linear = {"extra": {"database_path": str(bridge), "oauth_file": str(tmp / "o.json"),
                        "outbound_mcp": {"ledger_path": str(mcp)}}} if with_linear else {}
    (tmp / "config.yaml").write_text(json.dumps({"gateway": {"platforms": {"linear": linear}}}))
    return tmp


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = diag.main(argv)
    return code, out.getvalue(), err.getvalue()


class LinearDiagnosticsTests(unittest.TestCase):
    def test_ledger_summary_is_read_only_and_truncates_keys(self):
        with tempfile.TemporaryDirectory() as t:
            home = make_home(Path(t))
            before = (home / "bridge.sqlite3").read_bytes()
            code, out, _ = run(["--home", str(home), "--json", "ledger"])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data["bridge"]["outbox"], {"delivered": 1, "dead": 1})
            self.assertEqual([r["id"] for r in data["open_outbox"]], ["b"])
            self.assertEqual(data["outbound_mcp"], {"outcome_unknown": 1})
            self.assertNotIn("SECRET", out)
            self.assertEqual((home / "bridge.sqlite3").read_bytes(), before)
            self.assertEqual(diag.local_binding(diag.load_platform(home), "issue-1"), "sess-1")

    def test_ro_connection_rejects_writes(self):
        with tempfile.TemporaryDirectory() as t:
            home = make_home(Path(t))
            con = diag._ro(str(home / "bridge.sqlite3"))
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM outbox")
            con.close()

    def test_unconfigured_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            home = make_home(Path(t), with_linear=False)
            code, out, err = run(["--home", str(home), "ledger"])
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(json.loads(err)["reason"], "linear_platform_not_configured")

    def test_profile_or_home_is_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            diag.main(["ledger"])


if __name__ == "__main__":
    unittest.main()
