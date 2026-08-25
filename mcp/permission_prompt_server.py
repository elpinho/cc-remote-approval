#!/usr/bin/env python3
"""
Minimal MCP server implementing Claude Code's `--permission-prompt-tool` contract
for headless (`claude -p`) sessions.

Unlike the PermissionRequest hook (which only runs alongside an interactive TTY
dialog and never fires in headless mode — confirmed empirically, no TTY means
no hook invocation at all), this is a real MCP tool Claude calls directly when
it needs permission during a --print run. It reuses the same Telegram
send/poll/resolve logic as hooks/permission_request.py so both paths share one
implementation of "what an approval message looks like".

The exact --permission-prompt-tool wire format isn't in Anthropic's public
docs. This implementation is deliberately defensive: it logs the raw
tools/call arguments before doing anything else, and reads tool_name/input
under a few plausible key spellings, so a schema mismatch shows up in the log
instead of failing silently.

Wire protocol: MCP over stdio, newline-delimited JSON-RPC 2.0 (no
Content-Length framing — that's LSP, not MCP).
"""
import json
import os
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PLUGIN_ROOT)
sys.path.insert(0, os.path.join(_PLUGIN_ROOT, "hooks"))

from utils.common import load_config, make_logger  # noqa: E402
from utils.channel import create_channel  # noqa: E402
from permission_request import (  # noqa: E402
    format_tool_display, send_approval_message, poll_callback,
    edit_message_resolved, build_ask_user_question_message,
    poll_question_answer, build_approval_buttons,
    classify_notion_write, handle_notion_decision,
)
from utils.common import send_full_tool  # noqa: E402

_log = make_logger("permission_prompt_mcp")

TOOL_NAME = "approve"
PROTOCOL_VERSION = "2024-11-05"


def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _reply(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _reply_error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle_initialize(req_id, params):
    _reply(req_id, {
        "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "cc-remote-approval-permission-prompt", "version": "0.1.0"},
    })


def _handle_tools_list(req_id):
    _reply(req_id, {"tools": [{
        "name": TOOL_NAME,
        "description": "Ask the user (via Telegram) whether a tool call should be allowed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "input": {"type": "object"},
            },
            "additionalProperties": True,
        },
    }]})


def _extract_tool_call(args):
    """Best-effort extraction — the real key names aren't publicly documented.
    Logs the raw payload so a mismatch is visible instead of silently wrong."""
    tool_name = (args.get("tool_name") or args.get("toolName")
                 or args.get("name") or "?")
    tool_input = (args.get("input") or args.get("tool_input")
                  or args.get("toolInput") or {})
    return tool_name, tool_input


def _decide_ask_user_question(ch, tool_name, tool_input):
    """AskUserQuestion needs its option buttons, not a bare Allow/Deny —
    otherwise 'allow' just re-runs the tool with no answer filled in."""
    try:
        msg_id, question_text, options, multi = build_ask_user_question_message(
            ch, tool_input, context_lines=[], session_tag="headless", show_more=False)
    except Exception as e:
        _log(f"SEND FAILED: {e}")
        return {"behavior": "deny", "message": f"Failed to reach Telegram: {e}"}

    if msg_id is None:
        return {"behavior": "deny", "message": "Malformed AskUserQuestion input"}

    # Mutated in place by poll_question_answer as "Other" ForceReply prompts
    # are created — deleted below regardless of outcome so no dangling
    # "reply to this message" prompt is left in the chat.
    prompt_ids = []
    answer_type, answer_value = poll_question_answer(
        ch, msg_id, options, multi, transcript_path="", poll_start_size=0,
        prompt_ids=prompt_ids)
    _log(f"RESOLVED (question): {answer_type}={answer_value}")

    for pid in prompt_ids:
        try:
            ch.delete_message(pid)
        except Exception:
            pass

    if answer_type in ("option", "text"):
        selected = answer_value or ""
        edit_message_resolved(ch, msg_id, "allow", tool_name, f"{question_text}: {selected}")
        updated_input = dict(tool_input)
        updated_input["answers"] = {question_text: selected}
        return {"behavior": "allow", "updatedInput": updated_input}
    else:
        edit_message_resolved(ch, msg_id, "timeout", tool_name, question_text)
        return {"behavior": "deny", "message": "Question timed out with no answer"}


def _decide(args):
    _log(f"RAW tools/call arguments: {json.dumps(args, ensure_ascii=False)[:4000]}")
    tool_name, tool_input = _extract_tool_call(args)

    cfg = load_config()
    ch, err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable: {err}")
        return {"behavior": "deny", "message": "Telegram channel not configured"}

    if tool_name == "AskUserQuestion":
        return _decide_ask_user_question(ch, tool_name, tool_input)

    notion_kind = classify_notion_write(tool_name, tool_input)
    if notion_kind:
        # No transcript in headless mode — handle_notion_decision falls
        # back to tool_input fields alone for the message body. `state` is
        # just bookkeeping here (no signal handlers to feed, unlike the
        # interactive hook's main()).
        state = {"ch": ch, "msg_id": None, "tool_name": tool_name,
                 "tool_display": "", "resolved": False, "prompt_ids": [],
                 "is_photo": False}
        result = handle_notion_decision(ch, state, notion_kind, tool_name, tool_input,
                                        transcript_path="", poll_start_size=0,
                                        session_tag="headless", cfg=cfg)
        if result is None:
            return {"behavior": "deny", "message": "Failed to reach Telegram"}
        if result == "local":
            return {"behavior": "deny", "message": "Unexpected local response in headless mode"}
        return result

    tool_display = format_tool_display(tool_name, tool_input, limit=cfg["tool_display_max_chars"])

    try:
        msg_id = send_approval_message(
            ch, tool_name, tool_display, context_lines=[],
            permission_suggestions=None, session_tag="headless", show_more=True)
    except Exception as e:
        _log(f"SEND FAILED: {e}")
        return {"behavior": "deny", "message": f"Failed to reach Telegram: {e}"}

    def _on_more():
        _log("User clicked More")
        sent, total = send_full_tool(ch, msg_id, tool_name, tool_input)
        if sent == total:  # includes 0==0 — nothing to show, drop button
            if total == 0:
                _log("No full tool content to expand")
            ch.edit_buttons(msg_id, build_approval_buttons(None, show_more=False))
            return True
        _log(f"Full tool content incomplete ({sent}/{total}); keeping button")
        return False

    answer = poll_callback(ch, msg_id, transcript_path="", poll_start_size=0, on_more=_on_more)
    _log(f"RESOLVED: {answer}")

    if answer in ("allow", "always"):
        edit_message_resolved(ch, msg_id, "allow", tool_name, tool_display)
        return {"behavior": "allow"}
    elif answer == "deny":
        edit_message_resolved(ch, msg_id, "deny", tool_name, tool_display)
        return {"behavior": "deny", "message": "User denied via Telegram"}
    else:
        edit_message_resolved(ch, msg_id, "timeout", tool_name, tool_display)
        return {"behavior": "deny", "message": "Approval request timed out"}


def _handle_tools_call(req_id, params):
    name = params.get("name")
    args = params.get("arguments", {})
    if name != TOOL_NAME:
        _reply_error(req_id, -32602, f"unknown tool {name!r}")
        return
    decision = _decide(args)
    _reply(req_id, {"content": [{"type": "text", "text": json.dumps(decision)}]})


def main():
    _log("START")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _log(f"BAD JSON: {e}")
            continue

        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params", {}) or {}

        try:
            if method == "initialize":
                _handle_initialize(req_id, params)
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                _handle_tools_list(req_id)
            elif method == "tools/call":
                _handle_tools_call(req_id, params)
            elif method == "ping":
                _reply(req_id, {})
            elif req_id is not None:
                _reply_error(req_id, -32601, f"method not found: {method}")
        except Exception as e:
            _log(f"HANDLER ERROR ({method}): {e}")
            if req_id is not None:
                _reply_error(req_id, -32603, str(e))


if __name__ == "__main__":
    main()
