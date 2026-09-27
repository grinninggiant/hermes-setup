"""OPS-216: covered capabilities stay on their CLI; deprecated ad hoc snippets fail CI."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "components" / "README.md"

# Path-like chars only, so registry prose such as "sqlite3 …linear-bridge.sqlite3" is not a hit.
DEPRECATED = {
    "direct ledger sqlite3": re.compile(
        r"sqlite3\s+(?:-\S+\s+)*[\"']?[~/$\w.{}-]*(?:linear-bridge|linear-outbound-mcp|restart-coordinator/queue)\.sqlite3"),
    "raw Linear GraphQL": re.compile(r"(?:curl|urlopen|Request\(|requests\.post)[^\n]*api\.linear\.app/graphql"),
}
SCANNED = [ROOT / "docs", ROOT / "skills", ROOT / "components"]


class CliCapabilityRegistryTests(unittest.TestCase):
    def test_registry_lists_supported_commands(self):
        text = REGISTRY.read_text(encoding="utf-8")
        for command in ("linear_diagnostics.py --profile P ledger", "restartctl.py status", "derya-gh", "ntn"):
            self.assertIn(command, text)
        for relative in ("platforms/linear-agent-platform/linear_diagnostics.py",
                         "operations/gateway-restart-coordinator/gateway_restartctl.py"):
            self.assertTrue((ROOT / "components" / relative).is_file(), relative)

    def test_patterns_catch_real_snippets_but_not_registry_prose(self):
        self.assertTrue(DEPRECATED["direct ledger sqlite3"].search(
            "sqlite3 -readonly ~/.hermes/profiles/general/state/linear-bridge.sqlite3 'select 1'"))
        self.assertTrue(DEPRECATED["raw Linear GraphQL"].search(
            "curl -s https://api.linear.app/graphql -d @q.json"))
        registry = REGISTRY.read_text(encoding="utf-8")
        self.assertFalse(any(p.search(registry) for p in DEPRECATED.values()))

    def test_docs_and_skills_use_supported_cli(self):
        hits = []
        for base in SCANNED:
            for path in base.rglob("*.md"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                hits += [f"{path.relative_to(ROOT)}: {name}" for name, p in DEPRECATED.items() if p.search(text)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
