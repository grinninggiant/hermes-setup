"""Acceptance-criteria parsing and fail-closed completion gates."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timezone

from markdown_it import MarkdownIt

_CHECKBOX_RE = re.compile(r"^[ \t]*[-+*][ \t]+\[([ xX])\][ \t]+(.+?)[ \t]*$")
_ACCEPTANCE_HEADINGS = {"acceptance", "kabul kriterleri", "acceptance criteria"}
_COMMONMARK = MarkdownIt("commonmark")


def _is_acceptance_heading(tokens: list, index: int) -> bool:
    token = tokens[index]
    return bool(
        token.type == "heading_open" and token.tag == "h2" and token.level == 0
        and token.map and index + 1 < len(tokens)
        and tokens[index + 1].type == "inline"
        and " ".join(str(tokens[index + 1].content or "").casefold().split())
        in _ACCEPTANCE_HEADINGS
    )


def acceptance_section_error(
    description: str, criteria: tuple[AcceptanceCriterion, ...] | None = None,
) -> str | None:
    """Reject ambiguous or empty explicit acceptance sections; absent means no gate."""
    tokens = _COMMONMARK.parse(description) if isinstance(description, str) else []
    count = sum(_is_acceptance_heading(tokens, index) for index in range(len(tokens)))
    if count > 1:
        return "acceptance_section_ambiguous"
    if count == 1 and not (acceptance_criteria(description) if criteria is None else criteria):
        return "acceptance_section_empty"
    return None


@dataclass(frozen=True)
class AcceptanceCriterion:
    text: str
    checked: bool
    criterion_hash: str


@dataclass(frozen=True)
class AcceptanceGateResult:
    allowed: bool
    reason: str
    criteria: tuple[AcceptanceCriterion, ...]


def acceptance_criteria(description: str) -> tuple[AcceptanceCriterion, ...]:
    """Return genuine bullet task items from the canonical acceptance section."""
    if not isinstance(description, str) or not description:
        return ()

    tokens = _COMMONMARK.parse(description)
    section_start: int | None = None
    section_end: int | None = None
    heading_count = 0
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != "h2" or token.level != 0 or not token.map:
            continue
        is_acceptance = _is_acceptance_heading(tokens, index)
        heading_count += is_acceptance
        if section_start is None:
            if is_acceptance:
                section_start = int(token.map[1])
            continue
        if section_end is None:
            section_end = int(token.map[0])
    if section_start is None or heading_count != 1:
        return ()

    lines = description.splitlines()
    if section_end is None:
        section_end = len(lines)
    checkbox_lines: set[int] = set()
    list_stack: list[str] = []
    for token in tokens:
        if token.type == "bullet_list_open":
            list_stack.append("bullet")
        elif token.type == "ordered_list_open":
            list_stack.append("ordered")
        elif token.type == "list_item_open" and list_stack and list_stack[-1] == "bullet":
            if token.map:
                line_index = int(token.map[0])
                if section_start <= line_index < section_end:
                    checkbox_lines.add(line_index)
        elif token.type in {"bullet_list_close", "ordered_list_close"} and list_stack:
            list_stack.pop()

    result: list[AcceptanceCriterion] = []
    text_occurrences: dict[str, int] = {}
    for line_index in sorted(checkbox_lines):
        if line_index >= len(lines):
            continue
        match = _CHECKBOX_RE.match(lines[line_index])
        if match is None:
            continue
        text = " ".join(match.group(2).split())
        if not text:
            continue
        occurrence = text_occurrences.get(text, 0) + 1
        text_occurrences[text] = occurrence
        identity = f"{text}\0{occurrence}"
        result.append(
            AcceptanceCriterion(
                text=text,
                checked=match.group(1).casefold() == "x",
                criterion_hash=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(result)


def validate_evidence_envelope(envelope: object) -> dict[str, str] | None:
    """Accept only metadata-safe, qualifying PASS evidence."""
    if not isinstance(envelope, dict):
        return None
    required = {
        "criterion_hash", "test_class", "evidence_digest", "evidence_pointer",
        "observed_revision", "result", "timestamp",
    }
    if set(envelope) != required:
        return None
    normalized = {key: str(envelope.get(key) or "") for key in required}
    qualifying = {"integration", "e2e", "live", "vendor", "file", "api", "runtime", "security"}
    hex_chars = frozenset("0123456789abcdef")
    try:
        observed_timestamp = datetime.fromisoformat(
            normalized["observed_revision"].replace("Z", "+00:00")
        )
        evidence_timestamp = datetime.fromisoformat(
            normalized["timestamp"].replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if (
        normalized["result"] != "PASS"
        or normalized["test_class"] not in qualifying
        or len(normalized["criterion_hash"]) != 64
        or any(character not in hex_chars for character in normalized["criterion_hash"])
        or len(normalized["evidence_digest"]) != 64
        or any(character not in hex_chars for character in normalized["evidence_digest"])
        or not normalized["evidence_pointer"].startswith(
            ("linear://", "artifact://", "vendor://", "sha256:")
        )
        or len(normalized["evidence_pointer"]) > 500
        or any(
            character.isspace() or ord(character) < 0x20
            for character in normalized["evidence_pointer"]
        )
        or not normalized["observed_revision"]
        or len(normalized["observed_revision"]) > 200
        or not normalized["timestamp"]
        or len(normalized["timestamp"]) > 200
        or observed_timestamp.tzinfo is None
        or evidence_timestamp.tzinfo is None
    ):
        return None
    return normalized


EvidenceResolver = Callable[[str], Mapping[str, object] | None]


def authenticate_evidence_envelope(
    envelope: object,
    *,
    issue_id: str,
    delegate_id: str,
    resolver: EvidenceResolver | None,
    expected_agent_session_id: str | None = None,
    expected_hermes_turn_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, str] | None:
    """Resolve a pointer server-side and bind its trusted metadata to one criterion.

    The model-provided envelope is only a claim. The resolver is the authority for
    pointer ownership and digest metadata; absence, ambiguity, or any mismatch
    fails closed. Evidence bytes remain outside this ledger.
    """
    normalized = validate_evidence_envelope(envelope)
    if (
        normalized is None or resolver is None
        or not all(isinstance(value, str) and value for value in (
            issue_id, delegate_id, expected_agent_session_id, expected_hermes_turn_id,
        ))
    ):
        return None
    try:
        resolved = resolver(normalized["evidence_pointer"])
    except Exception:
        return None
    required = {
        "evidence_pointer",
        "issue_id",
        "delegate_id",
        "agent_session_id",
        "hermes_turn_id",
        "criterion_hash",
        "evidence_digest",
        "observed_revision",
        "timestamp",
    }
    if not isinstance(resolved, Mapping) or set(resolved) != required:
        return None
    trusted: dict[str, str] = {}
    for key in required:
        value = resolved[key]
        if not isinstance(value, str) or not value:
            return None
        trusted[key] = value
    expected = {
        "evidence_pointer": normalized["evidence_pointer"],
        "issue_id": issue_id,
        "delegate_id": delegate_id,
        "agent_session_id": expected_agent_session_id,
        "hermes_turn_id": expected_hermes_turn_id,
        "criterion_hash": normalized["criterion_hash"],
        "evidence_digest": normalized["evidence_digest"],
        "observed_revision": normalized["observed_revision"],
        "timestamp": normalized["timestamp"],
    }
    if any(not hmac.compare_digest(trusted[key].encode("utf-8"), value.encode("utf-8"))
           for key, value in expected.items()):
        return None
    try:
        observed_at = datetime.fromisoformat(
            trusted["observed_revision"].replace("Z", "+00:00")
        )
        evidence_at = datetime.fromisoformat(trusted["timestamp"].replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return None
    if (
        observed_at.tzinfo is None
        or evidence_at.tzinfo is None
        or observed_at > evidence_at
        or evidence_at > current
    ):
        return None
    return normalized


def acceptance_gate(
    description: str,
    evidence_hashes: AbstractSet[str],
) -> AcceptanceGateResult:
    criteria = acceptance_criteria(description)
    section_error = acceptance_section_error(description, criteria)
    if section_error is not None:
        return AcceptanceGateResult(False, section_error, criteria)
    if not criteria:
        return AcceptanceGateResult(True, "no_acceptance_criteria", criteria)
    if any(not criterion.checked for criterion in criteria):
        return AcceptanceGateResult(False, "acceptance_unchecked", criteria)
    if any(criterion.criterion_hash not in evidence_hashes for criterion in criteria):
        return AcceptanceGateResult(False, "acceptance_evidence_incomplete", criteria)
    return AcceptanceGateResult(True, "acceptance_complete", criteria)
