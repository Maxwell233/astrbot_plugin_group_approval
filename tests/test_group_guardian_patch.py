"""Compatibility-patch contract tests (GroupGuardian moderation precedence).

The deployment requirement for BUG-001 is that GroupGuardian's main moderation handler
runs before this plugin's admission gate (priority 50). We ship a patch rather than mutating
another plugin's handlers at runtime. These tests guard the patch content so it cannot
silently regress or drift from GATE_PRIORITY.

They do NOT execute GroupGuardian; real-registry ordering is covered by
tests/test_framework_handler_order.py under a real AstrBot install.
"""

from __future__ import annotations

from pathlib import Path

from approval.gate import GATE_PRIORITY, MODERATION_RECOMMENDED_PRIORITY

PATCH_PATH = Path(__file__).resolve().parents[1] / "patches" / "group_guardian_priority.patch"


def test_patch_exists_and_targets_the_main_moderation_handler():
    text = PATCH_PATH.read_text(encoding="utf-8")
    assert "--- a/main.py" in text and "+++ b/main.py" in text
    added = [line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    removed = [line[1:] for line in text.splitlines() if line.startswith("-") and not line.startswith("---")]
    joined_added = "\n".join(added)

    # Exactly the moderation-handler decorator changes; nothing else is touched.
    assert [line.strip() for line in removed] == ["@filter.event_message_type(filter.EventMessageType.ALL)"]

    assert f"priority={MODERATION_RECOMMENDED_PRIORITY}" in joined_added
    assert "async def _handle_message" in text  # context line of the hunk
    # The patch must not alter other handlers' decorators.
    assert joined_added.count("@filter.event_message_type(") == 1


def test_patch_priority_is_above_the_gate():
    text = PATCH_PATH.read_text(encoding="utf-8")
    assert MODERATION_RECOMMENDED_PRIORITY > GATE_PRIORITY
    assert f"priority={MODERATION_RECOMMENDED_PRIORITY}" in text
    assert "GATE_PRIORITY" in text


def test_patch_preserves_upstream_license_note():
    """GroupGuardian is MIT; the patch documentation must keep attribution."""
    readme = PATCH_PATH.parent / "README.md"
    assert readme.exists(), "patches/README.md documenting license/deployment is required"
    content = readme.read_text(encoding="utf-8")
    assert "MIT" in content
    assert "zhaisir" in content or "zcj-ui" in content
