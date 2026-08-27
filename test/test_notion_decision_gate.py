"""Regression tests for the rich Notion decision gate on the *interactive hook*
path — i.e. with a real transcript_path.

Why this file exists: every pre-existing test of the Notion decision feature
exercised `classify_notion_write` (30 references) but nothing ever called
`handle_notion_decision`, and the headless MCP server — the only production
caller anyone tested end to end — always passes `transcript_path=""`, which
short-circuits `_rationale_from_transcript` before it does any work. That left
the transcript-reading branch completely uncovered, and a missing
`extract_last_messages` import in it crashed the hook after logging
NOTION CLASSIFY and before sending anything. Claude Code reads a crashed hook
as a denial, so two production Notion writes were auto-denied on 2026-08-27
with no Telegram message and no error anyone could see.

The invariant these tests pin down: with a transcript present, the gate still
sends exactly one message and returns the reviewer's decision.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

from scenarios import FakeChannel


CHARACTER_INPUT = {
    "pages": [{
        "properties": {
            "Name": "Tally",
            "Creature type": "small spined lookout-creature",
            "Region": ["Rafters"],
            "Personality traits": "obsessive record-keeper",
        },
        "content": "A watcher three levels up.",
    }],
    "parent": {"database_id": "3bf130c120d680859b87e79ce6bc744f"},
}

CONTENT_EDIT_INPUT = {
    "page_id": "3bf130c120d680f6800cf9cd361afdd7",
    "command": "update_content",
    "content_updates": [{"old_str": "## Open Threads", "new_str": "## A Beat from the Rafters"}],
}


def _transcript(tmp_path):
    """A minimal transcript in the shape extract_last_messages expects: the
    calling agent's rationale as the last assistant message."""
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"type": "user", "message": {"role": "user", "content": "Run the Story Writer stage"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Proposing Tally, a Rafters watcher, because the roster is empty."}]}},
    ]) + "\n")
    return path


def _approving_channel():
    ch = FakeChannel()
    ch.poll = lambda *_a, **_k: {"type": "callback", "data": "allow"}
    return ch


class TestNotionDecisionWithTranscript:
    """The interactive-hook path: transcript_path is a real file."""

    def test_character_proposal_sends_and_allows(self, tmp_path):
        from permission_request import handle_notion_decision

        ch = _approving_channel()
        state = {}
        result = handle_notion_decision(
            ch, state, "character_proposal", "mcp__notion__notion-create-pages",
            CHARACTER_INPUT, str(_transcript(tmp_path)), 0, "", {"context_turns": 3})

        assert result == {"behavior": "allow"}
        assert state.get("msg_id") is not None, "gate must actually send a message"

    def test_content_edit_sends_and_allows(self, tmp_path):
        from permission_request import handle_notion_decision

        ch = _approving_channel()
        state = {}
        result = handle_notion_decision(
            ch, state, "content_edit", "mcp__notion__notion-update-page",
            CONTENT_EDIT_INPUT, str(_transcript(tmp_path)), 0, "", {"context_turns": 3})

        assert result == {"behavior": "allow"}
        assert state.get("msg_id") is not None

    def test_rationale_from_transcript_is_included(self, tmp_path):
        """The whole point of reading the transcript: the reviewer sees the
        agent's stated reasoning, not just a property dump."""
        from permission_request import _rationale_from_transcript

        rationale = _rationale_from_transcript(str(_transcript(tmp_path)))
        assert "Rafters watcher" in rationale

    def test_unreadable_transcript_degrades_instead_of_crashing(self, tmp_path):
        """A missing/garbled transcript costs us the rationale line — it must
        never take down the gate itself."""
        from permission_request import handle_notion_decision

        ch = _approving_channel()
        state = {}
        result = handle_notion_decision(
            ch, state, "character_proposal", "mcp__notion__notion-create-pages",
            CHARACTER_INPUT, str(tmp_path / "does-not-exist.jsonl"), 0, "",
            {"context_turns": 3})

        assert result == {"behavior": "allow"}
        assert state.get("msg_id") is not None

    def test_retry_and_cancel_carry_their_machine_readable_prefixes(self, tmp_path):
        """Calling agents branch on these prefixes, so they are part of the
        contract, not cosmetic."""
        from permission_request import handle_notion_decision

        for data, prefix in (("retry", "RETRY:"), ("deny", "CANCEL:")):
            ch = FakeChannel()
            ch.poll = lambda *_a, _d=data, **_k: {"type": "callback", "data": _d}
            result = handle_notion_decision(
                ch, {}, "character_proposal", "mcp__notion__notion-create-pages",
                CHARACTER_INPUT, str(_transcript(tmp_path)), 0, "", {"context_turns": 3})

            assert result["behavior"] == "deny"
            assert result["message"].startswith(prefix)


class TestHookModuleImportsAreComplete:
    """Cheap guard against the exact class of bug that caused the outage: a
    name used at runtime but never imported. Anything reachable only on an
    untested branch won't be caught by the tests above."""

    def test_no_unresolved_globals(self):
        import builtins
        import permission_request as pr

        module_names = set(vars(pr)) | set(dir(builtins))
        unresolved = set()
        for obj in vars(pr).values():
            code = getattr(obj, "__code__", None)
            if code is None or code.co_filename != pr.__file__:
                continue
            for name in code.co_names:
                if name.isidentifier() and name not in module_names:
                    unresolved.add(name)

        # co_names also collects attribute names (e.g. ``.get``), so filter to
        # names that are actually called as bare globals somewhere in source.
        source = open(pr.__file__).read()
        real = {n for n in unresolved if f"\n    {n}(" in source or f" {n}(" in source}
        assert not real, f"used but never imported/defined: {sorted(real)}"
