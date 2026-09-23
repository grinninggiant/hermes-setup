from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from acceptance import (  # noqa: E402
    acceptance_criteria,
    acceptance_gate,
    authenticate_evidence_envelope,
)
from ledger import DeliveryLedger  # noqa: E402


class AcceptanceParserTests(unittest.TestCase):
    def test_parser_limits_checkboxes_to_acceptance_section_and_hashes_exact_text(self):
        description = """## Plan
- [ ] Not acceptance

## Kabul kriterleri
- [x] First criterion
- [ ] Second criterion

## Notes
- [ ] Not acceptance either
"""
        criteria = acceptance_criteria(description)
        self.assertEqual([item.text for item in criteria], ["First criterion", "Second criterion"])
        self.assertEqual([item.checked for item in criteria], [True, False])
        self.assertTrue(all(len(item.criterion_hash) == 64 for item in criteria))

    def test_current_criteria_ignore_fenced_history_and_quoted_headings(self):
        history = "\n".join(f"- [ ] Historical {i}" for i in range(10))
        active = "\n".join(f"- [x] Current {i}" for i in range(16))
        description = (
            f"## History\n````markdown\n## Kabul kriterleri\n{history}\n````\n"
            f"## Acceptance\n{active}\n\n> ## Notes\n\n- [ ] Still required\n"
        )
        criteria = acceptance_criteria(description)
        self.assertEqual(len(criteria), 17)
        self.assertFalse(any(item.text.startswith("Historical") for item in criteria))
        self.assertEqual(acceptance_gate(description, set()).reason, "acceptance_unchecked")
        self.assertEqual(len(acceptance_criteria(description.rsplit("\n\n> ## Notes", 1)[0])), 16)

    def test_duplicate_criterion_text_has_occurrence_scoped_identity(self):
        criteria = acceptance_criteria(
            "## Kabul kriterleri\n- [ ] Same criterion\n- [ ] Same criterion"
        )
        self.assertEqual(len(criteria), 2)
        self.assertNotEqual(criteria[0].criterion_hash, criteria[1].criterion_hash)

    def test_identity_survives_neighbor_insertion_and_reordering(self):
        original = acceptance_criteria("## Kabul kriterleri\n- [ ] Alpha\n- [ ] Beta")
        edited = acceptance_criteria("## Kabul kriterleri\n- [ ] New\n- [ ] Beta\n- [ ] Alpha")
        self.assertEqual(
            {item.text: item.criterion_hash for item in original},
            {item.text: item.criterion_hash for item in edited if item.text != "New"},
        )

    def test_parser_ignores_fenced_indented_and_prose_checkbox_examples(self):
        description = """## Acceptance criteria
- [x] Real criterion

```text
- [ ] Fenced example
```

    - [ ] Indented code example

Prose example: - [ ] Not a task item
"""
        criteria = acceptance_criteria(description)
        self.assertEqual([item.text for item in criteria], ["Real criterion"])
        self.assertEqual([item.checked for item in criteria], [True])

    def test_gate_requires_all_checked_and_pass_evidence_for_exact_delegate(self):
        description = "## Kabul kriterleri\n- [x] First\n- [x] Second"
        criteria = acceptance_criteria(description)
        evidence = {criteria[0].criterion_hash}
        denied = acceptance_gate(description, evidence)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "acceptance_evidence_incomplete")
        allowed = acceptance_gate(description, {item.criterion_hash for item in criteria})
        self.assertTrue(allowed.allowed)

    def test_gate_denies_unchecked_but_allows_issues_without_acceptance_checkboxes(self):
        self.assertEqual(
            acceptance_gate("## Kabul kriterleri\n- [ ] Pending", set()).reason,
            "acceptance_unchecked",
        )
        self.assertTrue(acceptance_gate("No criteria", set()).allowed)

    def test_gate_rejects_duplicate_or_empty_acceptance_sections(self):
        duplicate = "## Acceptance\nContext only\n## Notes\nText\n## Kabul kriterleri\n- [ ] Real work"
        self.assertEqual(acceptance_gate(duplicate, set()).reason, "acceptance_section_ambiguous")
        self.assertFalse(acceptance_gate(duplicate, set()).allowed)
        self.assertEqual(
            acceptance_gate("## Acceptance\nNo tasks yet", set()).reason,
            "acceptance_section_empty",
        )
        self.assertTrue(acceptance_gate("## Notes\n- [ ] Unrelated", set()).allowed)

    def test_evidence_authentication_fails_closed_without_server_resolver(self):
        envelope = {
            "criterion_hash": "a" * 64,
            "test_class": "integration",
            "evidence_digest": "b" * 64,
            "evidence_pointer": "linear://activity/evidence-1",
            "observed_revision": "2026-08-30T20:00:00.000Z",
            "result": "PASS",
            "timestamp": "2026-08-30T20:00:01.000Z",
        }
        self.assertIsNone(
            authenticate_evidence_envelope(
                envelope,
                issue_id="issue-1",
                delegate_id="delegate-1",
                resolver=None,
                now=datetime(2026, 8, 30, 20, 0, 2, tzinfo=timezone.utc),
            )
        )

    def test_evidence_authentication_binds_pointer_owner_issue_delegate_criterion_and_digest(self):
        envelope = {
            "criterion_hash": "a" * 64,
            "test_class": "integration",
            "evidence_digest": "b" * 64,
            "evidence_pointer": "linear://activity/evidence-1",
            "observed_revision": "2026-08-30T20:00:00.000Z",
            "result": "PASS",
            "timestamp": "2026-08-30T20:00:01.000Z",
        }
        trusted = {
            "evidence_pointer": envelope["evidence_pointer"],
            "issue_id": "issue-1",
            "delegate_id": "delegate-1",
            "agent_session_id": "session-current",
            "hermes_turn_id": "turn-current",
            "criterion_hash": envelope["criterion_hash"],
            "evidence_digest": envelope["evidence_digest"],
            "observed_revision": envelope["observed_revision"],
            "timestamp": envelope["timestamp"],
        }
        now = datetime(2026, 8, 30, 20, 0, 2, tzinfo=timezone.utc)
        self.assertEqual(
            authenticate_evidence_envelope(
                envelope,
                issue_id="issue-1",
                delegate_id="delegate-1",
                resolver=lambda _pointer: dict(trusted),
                expected_agent_session_id="session-current",
                expected_hermes_turn_id="turn-current",
                now=now,
            ),
            envelope,
        )
        self.assertIsNone(authenticate_evidence_envelope(
            {**envelope, "hermes_turn_id": "turn-current"},
            issue_id="issue-1", delegate_id="delegate-1",
            resolver=lambda _pointer: dict(trusted),
            expected_agent_session_id="session-current",
            expected_hermes_turn_id="turn-current",
            now=now,
        ))
        for expected_session, expected_turn in ((None, "turn-current"),
                                                 ("session-current", None)):
            self.assertIsNone(authenticate_evidence_envelope(
                envelope,
                issue_id="issue-1", delegate_id="delegate-1",
                resolver=lambda _pointer: dict(trusted),
                expected_agent_session_id=expected_session,
                expected_hermes_turn_id=expected_turn,
                now=now,
            ))
        for missing in ("agent_session_id", "hermes_turn_id"):
            with self.subTest(missing=missing):
                self.assertIsNone(authenticate_evidence_envelope(
                    envelope,
                    issue_id="issue-1",
                    delegate_id="delegate-1",
                    resolver=lambda _pointer, key=missing: {
                        key_: value for key_, value in trusted.items() if key_ != key
                    },
                    expected_agent_session_id="session-current",
                    expected_hermes_turn_id="turn-current",
                    now=now,
                ))
        for field, forged in (
            ("evidence_pointer", "linear://activity/other"),
            ("issue_id", "issue-other"),
            ("delegate_id", "delegate-other"),
            ("agent_session_id", "session-other"),
            ("hermes_turn_id", "turn-other"),
            ("criterion_hash", "c" * 64),
            ("evidence_digest", "d" * 64),
            ("observed_revision", "2026-08-30T19:59:59.000Z"),
            ("timestamp", "2026-08-30T20:00:00.500Z"),
        ):
            with self.subTest(field=field):
                resolution = {**trusted, field: forged}
                self.assertIsNone(
                    authenticate_evidence_envelope(
                        envelope,
                        issue_id="issue-1",
                        delegate_id="delegate-1",
                        resolver=lambda _pointer, value=resolution: value,
                        expected_agent_session_id="session-current",
                        expected_hermes_turn_id="turn-current",
                        now=now,
                    )
                )

    def test_evidence_authentication_rejects_stringlike_resolver_metadata(self):
        class StringLike:
            def __init__(self, value):
                self.value = value

            def __str__(self):
                return self.value

        envelope = {
            "criterion_hash": "a" * 64,
            "test_class": "integration",
            "evidence_digest": "b" * 64,
            "evidence_pointer": "linear://activity/evidence-1",
            "observed_revision": "2026-08-30T20:00:00.000Z",
            "result": "PASS",
            "timestamp": "2026-08-30T20:00:01.000Z",
        }
        trusted = {
            "evidence_pointer": envelope["evidence_pointer"],
            "issue_id": "issue-1",
            "delegate_id": "delegate-1",
            "agent_session_id": "session-current",
            "hermes_turn_id": "turn-current",
            "criterion_hash": envelope["criterion_hash"],
            "evidence_digest": envelope["evidence_digest"],
            "observed_revision": envelope["observed_revision"],
            "timestamp": envelope["timestamp"],
        }
        now = datetime(2026, 8, 30, 20, 0, 2, tzinfo=timezone.utc)
        for field, value in trusted.items():
            with self.subTest(field=field):
                forged = {**trusted, field: StringLike(value)}
                self.assertIsNone(authenticate_evidence_envelope(
                    envelope, issue_id="issue-1", delegate_id="delegate-1",
                    resolver=lambda _pointer, record=forged: record,
                    expected_agent_session_id="session-current",
                    expected_hermes_turn_id="turn-current", now=now,
                ))

    def test_evidence_authentication_rejects_future_and_causally_impossible_timestamps(self):
        base = {
            "criterion_hash": "a" * 64,
            "test_class": "integration",
            "evidence_digest": "b" * 64,
            "evidence_pointer": "linear://activity/evidence-1",
            "observed_revision": "2026-08-30T20:00:00.000Z",
            "result": "PASS",
            "timestamp": "2026-08-30T20:00:01.000Z",
        }
        now = datetime(2026, 8, 30, 20, 0, 2, tzinfo=timezone.utc)
        for observed, timestamp in (
            ("2026-08-30T20:00:03.000Z", "2026-08-30T20:00:01.000Z"),
            ("2026-08-30T20:00:00.000Z", "2026-08-30T20:00:03.000Z"),
        ):
            with self.subTest(observed=observed, timestamp=timestamp):
                envelope = {**base, "observed_revision": observed, "timestamp": timestamp}
                resolution = {
                    "evidence_pointer": envelope["evidence_pointer"],
                    "issue_id": "issue-1",
                    "delegate_id": "delegate-1",
                    "agent_session_id": "session-current",
                    "hermes_turn_id": "turn-current",
                    "criterion_hash": envelope["criterion_hash"],
                    "evidence_digest": envelope["evidence_digest"],
                    "observed_revision": observed,
                    "timestamp": timestamp,
                }
                self.assertIsNone(
                    authenticate_evidence_envelope(
                        envelope,
                        issue_id="issue-1",
                        delegate_id="delegate-1",
                        resolver=lambda _pointer, value=resolution: value,
                        expected_agent_session_id="session-current",
                        expected_hermes_turn_id="turn-current",
                        now=now,
                    )
                )


class AcceptanceEvidenceLedgerTests(unittest.TestCase):
    def test_newly_verified_current_source_replaces_old_revision_but_carries_current_proof(self):
        old_revision = "2026-08-30T20:00:02.000Z"
        source_revision = "2026-08-30T20:00:04.000Z"  # Intervening issue edit.
        updated_at = "2026-08-30T20:00:05.000Z"  # Issue readback after marking.
        old_hash, new_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            ledger = DeliveryLedger(str(root / "ledger.sqlite3"), startup_recovery=False)
            try:
                for issue_id, actor_id in (
                    ("issue-1", "delegate-1"),
                    ("issue-1", "other-delegate"),
                    ("other-issue", "delegate-1"),
                ):
                    ledger.record_acceptance_evidence(
                        issue_id=issue_id, actor_id=actor_id,
                        criterion_hash=old_hash, test_class="integration",
                        evidence_digest="a" * 64,
                        evidence_pointer="linear://activity/old",
                        observed_revision="2026-08-30T20:00:00.000Z",
                        accepted_revision=old_revision, result="PASS",
                        timestamp="2026-08-30T20:00:01.000Z",
                    )
                ledger.record_acceptance_evidence(
                    issue_id="issue-1", actor_id="delegate-1",
                    criterion_hash="c" * 64, test_class="runtime",
                    evidence_digest="c" * 64,
                    evidence_pointer="artifact://native/source-proof",
                    observed_revision=source_revision,
                    accepted_revision=source_revision, result="PASS",
                    timestamp=source_revision,
                )
                with self.assertRaisesRegex(ValueError, "source revision changed"):
                    ledger.persist_acceptance_batch(
                        "issue-1", "delegate-1", from_revision=source_revision,
                        accepted_revision=updated_at,
                        evidence=[{
                            "criterion_hash": new_hash, "test_class": "runtime",
                            "evidence_digest": "b" * 64,
                            "evidence_pointer": "artifact://native/stale-test",
                            "observed_revision": old_revision, "result": "PASS",
                            "timestamp": source_revision,
                        }],
                    )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes("issue-1", "delegate-1"),
                    {old_hash, "c" * 64},
                )
                ledger.persist_acceptance_batch(
                    "issue-1", "delegate-1", from_revision=source_revision,
                    accepted_revision=updated_at,
                    evidence=[{
                        "criterion_hash": new_hash, "test_class": "runtime",
                        "evidence_digest": "b" * 64,
                        "evidence_pointer": "artifact://native/current-test",
                        "observed_revision": source_revision, "result": "PASS",
                        "timestamp": "2026-08-30T20:00:04.500Z",
                    }],
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=updated_at,
                    ), {new_hash, "c" * 64},
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes("issue-1", "delegate-1"),
                    {new_hash, "c" * 64},
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=old_revision,
                    ), set(),
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=source_revision,
                    ), set(),
                )
                for issue_id, actor_id in (
                    ("issue-1", "other-delegate"),
                    ("other-issue", "delegate-1"),
                ):
                    self.assertEqual(
                        ledger.acceptance_evidence_hashes(
                            issue_id, actor_id, accepted_revision=old_revision,
                        ), {old_hash},
                    )
            finally:
                ledger.close()

    def test_two_sequential_checkbox_passes_retain_current_proof(self):
        revisions = (
            "2026-08-30T20:00:00.000Z",
            "2026-08-30T20:00:01.000Z",
            "2026-08-30T20:00:02.000Z",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            ledger = DeliveryLedger(str(root / "ledger.sqlite3"), startup_recovery=False)
            try:
                for index, criterion_hash in enumerate(("a" * 64, "b" * 64)):
                    source, updated = revisions[index:index + 2]
                    ledger.persist_acceptance_batch(
                        "issue-1", "delegate-1", from_revision=source,
                        accepted_revision=updated,
                        evidence=[{
                            "criterion_hash": criterion_hash, "test_class": "runtime",
                            "evidence_digest": criterion_hash,
                            "evidence_pointer": f"artifact://native/criterion-{index}",
                            "observed_revision": source, "result": "PASS",
                            "timestamp": source,
                        }],
                    )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=revisions[2],
                    ), {"a" * 64, "b" * 64},
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=revisions[1],
                    ), set(),
                )
            finally:
                ledger.close()

    def test_preexisting_future_revision_blocks_replacement_without_deletion(self):
        source_revision = "2026-08-30T20:00:04.000Z"
        for stored_revision, timestamp in (
            ("2026-08-30T20:00:06.000Z", "2026-08-30T20:00:05.000Z"),
            ("2026-08-30T20:00:04+00:00", source_revision),
        ):
            with self.subTest(stored_revision=stored_revision), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                root.chmod(0o700)
                ledger = DeliveryLedger(str(root / "ledger.sqlite3"), startup_recovery=False)
                try:
                    ledger.record_acceptance_evidence(
                        issue_id="issue-1", actor_id="delegate-1",
                        criterion_hash="a" * 64, test_class="runtime",
                        evidence_digest="a" * 64,
                        evidence_pointer="artifact://native/concurrent",
                        observed_revision=source_revision, accepted_revision=stored_revision,
                        result="PASS", timestamp=timestamp,
                    )
                    with self.assertRaisesRegex(ValueError, "base revision changed"):
                        ledger.persist_acceptance_batch(
                            "issue-1", "delegate-1", from_revision=source_revision,
                            accepted_revision="2026-08-30T20:00:07.000Z",
                            evidence=[{
                                "criterion_hash": "b" * 64, "test_class": "runtime",
                                "evidence_digest": "b" * 64,
                                "evidence_pointer": "artifact://native/current",
                                "observed_revision": source_revision, "result": "PASS",
                                "timestamp": "2026-08-30T20:00:04.500Z",
                            }],
                        )
                    self.assertEqual(
                        ledger.acceptance_evidence_hashes("issue-1", "delegate-1"),
                        {"a" * 64},
                    )
                finally:
                    ledger.close()

    def test_pass_evidence_is_durable_metadata_only_and_delegate_scoped(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            path = root / "ledger.sqlite3"
            ledger = DeliveryLedger(str(path), startup_recovery=False)
            try:
                ledger.record_acceptance_evidence(
                    issue_id="issue-1",
                    criterion_hash="a" * 64,
                    actor_id="delegate-1",
                    test_class="integration",
                    evidence_digest="b" * 64,
                    evidence_pointer="linear://activity/evidence-1",
                    observed_revision="2026-08-30T20:00:00.000Z",
                    accepted_revision="2026-08-30T20:00:02.000Z",
                    result="PASS",
                    timestamp="2026-08-30T20:00:01.000Z",
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision="2026-08-30T20:00:02.000Z"
                    ),
                    {"a" * 64},
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision="stale-revision"
                    ),
                    set(),
                )
                self.assertEqual(ledger.acceptance_evidence_hashes("issue-1", "other"), set())
            finally:
                ledger.close()
            reopened = DeliveryLedger(str(path), startup_recovery=False)
            try:
                self.assertEqual(
                    reopened.acceptance_evidence_hashes("issue-1", "delegate-1"),
                    {"a" * 64},
                )
            finally:
                reopened.close()

    def test_partial_fail_and_malformed_evidence_are_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            ledger = DeliveryLedger(str(root / "ledger.sqlite3"), startup_recovery=False)
            try:
                for result in ("PARTIAL", "FAIL"):
                    with self.subTest(result=result), self.assertRaises(ValueError):
                        ledger.record_acceptance_evidence(
                            issue_id="issue-1",
                            criterion_hash="a" * 64,
                            actor_id="delegate-1",
                            test_class="integration",
                            evidence_digest="b" * 64,
                            evidence_pointer="linear://activity/evidence-1",
                            observed_revision="revision",
                            accepted_revision="accepted-revision",
                            result=result,
                            timestamp="timestamp",
                        )
            finally:
                ledger.close()

    def test_evidence_rejects_non_iso_metadata_and_does_not_offer_arbitrary_revision_carry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            ledger = DeliveryLedger(str(root / "ledger.sqlite3"), startup_recovery=False)
            try:
                with self.assertRaises(ValueError):
                    ledger.record_acceptance_evidence(
                        issue_id="issue-1", criterion_hash="a" * 64,
                        actor_id="delegate-1", test_class="integration",
                        evidence_digest="b" * 64,
                        evidence_pointer="linear://activity/evidence-1",
                        observed_revision="not-a-revision", accepted_revision="revision-1",
                        result="PASS", timestamp="not-a-timestamp",
                    )
                with self.assertRaises(ValueError):
                    ledger.record_acceptance_evidence(
                        issue_id="issue-1", criterion_hash="a" * 64,
                        actor_id="delegate-1", test_class="integration",
                        evidence_digest="b" * 64,
                        evidence_pointer="linear://activity/evidence-1",
                        observed_revision="2026-08-30T20:00:00.000Z",
                        accepted_revision="not-a-revision",
                        result="PASS", timestamp="2026-08-30T20:00:01.000Z",
                    )
                ledger.record_acceptance_evidence(
                    issue_id="issue-1", criterion_hash="a" * 64,
                    actor_id="delegate-1", test_class="integration",
                    evidence_digest="b" * 64,
                    evidence_pointer="linear://activity/evidence-1",
                    observed_revision="2026-08-30T20:00:00.000Z",
                    accepted_revision="2026-08-30T20:00:02.000Z",
                    result="PASS", timestamp="2026-08-30T20:00:01.000Z",
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1",
                        accepted_revision="2026-08-30T20:00:03.000Z",
                    ),
                    set(),
                )
                self.assertFalse(hasattr(ledger, "advance_acceptance_revision"))
            finally:
                ledger.close()

    def test_concurrent_legacy_schema_migration_is_serialized(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            path = root / "ledger.sqlite3"
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE acceptance_evidence ("
                "issue_id TEXT NOT NULL, criterion_hash TEXT NOT NULL, actor_id TEXT NOT NULL, "
                "test_class TEXT NOT NULL, evidence_digest TEXT NOT NULL, "
                "evidence_pointer TEXT NOT NULL, observed_revision TEXT NOT NULL, "
                "result TEXT NOT NULL, evidence_timestamp TEXT NOT NULL, created_at INTEGER NOT NULL, "
                "PRIMARY KEY(issue_id, criterion_hash, actor_id))"
            )
            db.execute("PRAGMA user_version=8")
            db.execute(
                "INSERT INTO acceptance_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "issue-legacy", "a" * 64, "delegate-1", "integration", "b" * 64,
                    "linear://activity/legacy", "2026-08-30T20:00:00.000Z", "PASS",
                    "2026-08-30T20:00:01.000Z", 1,
                ),
            )
            db.commit()
            db.close()
            path.chmod(0o600)
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def open_ledger() -> None:
                try:
                    barrier.wait()
                    ledger = DeliveryLedger(str(path), startup_recovery=False)
                    ledger.close()
                except sqlite3.Error as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=open_ledger) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            db = sqlite3.connect(path)
            try:
                columns = {row[1] for row in db.execute("PRAGMA table_info(acceptance_evidence)")}
                self.assertIn("accepted_revision", columns)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM acceptance_evidence").fetchone()[0],
                    0,
                )
            finally:
                db.close()

    def test_acceptance_batch_rolls_back_revision_and_all_new_evidence_atomically(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            ledger = DeliveryLedger(str(root / "ledger.sqlite3"), startup_recovery=False)
            old_revision = "2026-08-30T20:00:02.000Z"
            new_revision = "2026-08-30T20:00:03.000Z"
            try:
                ledger.record_acceptance_evidence(
                    issue_id="issue-1", criterion_hash="a" * 64,
                    actor_id="delegate-1", test_class="integration",
                    evidence_digest="a" * 64,
                    evidence_pointer="linear://activity/old",
                    observed_revision="2026-08-30T20:00:00.000Z",
                    accepted_revision=old_revision, result="PASS",
                    timestamp="2026-08-30T20:00:01.000Z",
                )
                with self.assertRaises(ValueError):
                    ledger.persist_acceptance_batch(
                        "issue-1", "delegate-1",
                        from_revision=old_revision,
                        accepted_revision=new_revision,
                        evidence=[
                            {
                                "criterion_hash": "b" * 64,
                                "test_class": "integration",
                                "evidence_digest": "b" * 64,
                                "evidence_pointer": "linear://activity/new",
                                "observed_revision": old_revision,
                                "result": "PASS",
                                "timestamp": "2026-08-30T20:00:02.500Z",
                            },
                            {
                                "criterion_hash": "c" * 64,
                                "test_class": "integration",
                                "evidence_digest": "c" * 64,
                                "evidence_pointer": "linear://activity/invalid",
                                "observed_revision": old_revision,
                                "result": "PASS",
                                "timestamp": "not-a-timestamp",
                            },
                        ],
                    )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=old_revision
                    ),
                    {"a" * 64},
                )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=new_revision
                    ),
                    set(),
                )
            finally:
                ledger.close()

    def test_delayed_acceptance_batch_cannot_regress_newer_evidence_revision(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            root.chmod(0o700)
            ledger = DeliveryLedger(str(root / "ledger.sqlite3"), startup_recovery=False)
            old_revision = "2026-08-30T20:00:02.000Z"
            new_revision = "2026-08-30T20:00:04.000Z"
            evidence = {
                "criterion_hash": "a" * 64,
                "test_class": "integration",
                "evidence_digest": "a" * 64,
                "evidence_pointer": "linear://activity/evidence-a",
                "observed_revision": old_revision,
                "result": "PASS",
                "timestamp": "2026-08-30T20:00:03.000Z",
            }
            try:
                ledger.record_acceptance_evidence(
                    issue_id="issue-1", actor_id="delegate-1",
                    criterion_hash=evidence["criterion_hash"],
                    test_class=evidence["test_class"],
                    evidence_digest=evidence["evidence_digest"],
                    evidence_pointer=evidence["evidence_pointer"],
                    observed_revision=evidence["observed_revision"],
                    accepted_revision=old_revision,
                    result=evidence["result"],
                    timestamp=evidence["timestamp"],
                )
                ledger.persist_acceptance_batch(
                    "issue-1", "delegate-1", from_revision=old_revision,
                    accepted_revision=new_revision, evidence=[evidence],
                )
                with self.assertRaisesRegex(ValueError, "base revision changed"):
                    ledger.persist_acceptance_batch(
                        "issue-1", "delegate-1", from_revision=old_revision,
                        accepted_revision="2026-08-30T20:00:03.500Z",
                        evidence=[evidence],
                    )
                self.assertEqual(
                    ledger.acceptance_evidence_hashes(
                        "issue-1", "delegate-1", accepted_revision=new_revision
                    ),
                    {"a" * 64},
                )
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
