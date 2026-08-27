#!/usr/bin/env python3
"""
PermissionRequest hook — escalate to Telegram after local timeout.
Parallel: native dialog shows immediately, Telegram fires after N seconds.
"""
import json
import os
import signal
import sys
import time
import traceback

from utils.common import (load_config, html_escape, make_logger,
                     mask_secrets, check_local_response, format_context_lines,
                     format_context_block, smart_truncate, POLL_TIMEOUT_SECONDS,
                     send_full_context, send_full_tool, extract_last_messages,
                     session_tag as common_session_tag)
from utils.channel import create_channel

_log = make_logger("permission_request")


def format_tool_display(tool_name, tool_input, limit=300):
    """Clean display of what the tool wants to do. Masks sensitive info.
    Truncates at line/word boundaries so multi-line commands don't show
    a mid-word cut in the middle of an argument."""
    if tool_name == "Bash":
        text = smart_truncate(tool_input.get("command", str(tool_input)), limit)
    elif tool_name in ("Edit", "Write"):
        text = smart_truncate(tool_input.get("file_path", str(tool_input)), limit)
    elif tool_name == "WebFetch":
        text = smart_truncate(tool_input.get("url", str(tool_input)), limit)
    elif tool_name in ("EnterPlanMode", "ExitPlanMode"):
        return ""
    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        return questions[0].get("question", "") if questions else ""
    else:
        raw = json.dumps(tool_input, ensure_ascii=False)
        text = smart_truncate(raw, limit) if raw != "{}" else ""
    return mask_secrets(text)


# Keycap emoji for option indices 1-10. AskUserQuestion schema currently
# caps at 4 options, so even 10 is generous headroom. Beyond 10 falls
# back to "(N)" — functional, just no special glyph.
_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def _format_option_body(options, multi, selected=None):
    """Render the option list as a text block for the message body.
    Each option shows its emoji number + full label + optional description."""
    selected = selected or set()
    lines = []
    for i, opt in enumerate(options):
        marker = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"{i+1}."
        label = opt.get("label", f"Option {i+1}")
        desc = opt.get("description", "")
        if multi:
            check = "✅ " if i in selected else "⬜ "
            line = f"{check}{marker} <b>{html_escape(label)}</b>"
        else:
            line = f"{marker} <b>{html_escape(label)}</b>"
        if desc:
            line += f"\n   <i>{html_escape(desc)}</i>"
        lines.append(line)
    return "\n".join(lines)


def build_ask_user_question_message(ch, tool_input, context_lines, session_tag="", show_more=True):
    """For AskUserQuestion: render options inline in the message body and
    use compact emoji-number buttons so long option labels don't get cut."""
    questions = tool_input.get("questions", [])
    if not questions:
        return None, None, None, False

    q = questions[0]
    # Cap question text before HTML assembly — if it's huge (AI-generated
    # questions can be), truncate cleanly at a paragraph boundary instead
    # of letting the final HTML-level hard cut slice through a tag.
    question_text = smart_truncate(q.get("question", "?"), 2000)
    options = q.get("options", [])
    multi = q.get("multiSelect", False)

    context_text = format_context_block(context_lines)

    options_block = _format_option_body(options, multi, selected=set())
    multi_hint = "\n\n<i>Multi-select: tap to toggle, then ✅ Submit</i>" if multi else ""
    tag = f" · <code>{html_escape(session_tag)}</code>" if session_tag else ""
    text = (
        f"❓ <b>{html_escape(question_text)}</b>{tag}\n\n"
        f"{options_block}"
        f"{multi_hint}"
        f"{context_text}"
    )
    text = smart_truncate(text, 4090, marker="\n\n<i>…truncated</i>")

    buttons = _build_question_keyboard(options, multi, selected=set(), show_more=show_more)
    msg_id = ch.send_message(text, buttons=buttons)
    return msg_id, question_text, options, multi


def _build_question_keyboard(options, multi, selected=None, show_more=True):
    """Build inline keyboard. Keycap emoji per option; labels live in the
    message body. Rows of up to 5 buttons to keep things compact on mobile."""
    selected = selected or set()
    row = []
    rows = []
    for i, _ in enumerate(options):
        marker = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"({i+1})"
        # In multi-select we prefix a check so the button reflects toggled state.
        text = f"{'✅ ' if i in selected else ''}{marker}" if multi else marker
        row.append({"text": text, "callback_data": f"opt:{i}"})
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if multi:
        rows.append([{"text": "✅ Submit", "callback_data": "opt:submit"}])
    rows.append([{"text": "✏️ Other", "callback_data": "opt:other"}])
    if show_more:
        rows.append([{"text": "📖 Full context", "callback_data": "opt:more"}])
    return rows


def poll_question_answer(ch, message_id, options, multi=False, transcript_path="", poll_start_size=0, prompt_ids=None, on_more=None):
    """Poll for AskUserQuestion answer via channel.

    prompt_ids: optional list, mutated in place as transient ForceReply
    prompts are created. Callers share this list with their cleanup
    handlers so stale prompts are deleted on any exit path (normal
    resolve, signal, atexit).

    on_more: optional callable invoked when user taps More. Called in-loop."""
    selected = set()
    # Msg ids we accept replies on. Starts with the question msg_id; the
    # "Other" branch appends the ForceReply prompt msg_id so TG clients
    # auto-quoting the prompt (the natural UX target) still route here.
    accepted_ids = [message_id]
    if prompt_ids is None:
        prompt_ids = []
    more_shown = True
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if check_local_response(transcript_path, poll_start_size, threshold=100):
            return "local", None

        update = ch.poll(accepted_ids)
        if update is None:
            time.sleep(1)
            continue

        # Close the TOCTOU window: transcript may have grown between the
        # last check and now. If the user responded locally, don't overwrite
        # their decision with the channel callback that arrived in parallel.
        if check_local_response(transcript_path, poll_start_size, threshold=100):
            return "local", None

        if update["type"] == "callback":
            data = update["data"]
            if not data.startswith("opt:"):
                continue
            action = data.split(":", 1)[1] if ":" in data else ""

            if action == "more":
                if on_more and more_shown:
                    # Flip first so rapid-duplicate callbacks drop, then
                    # restore on failure so the user can retry.
                    more_shown = False
                    if on_more(selected, multi) is False:
                        more_shown = True
                continue

            if action == "other":
                prompt_id = ch.send_reply_prompt(
                    message_id,
                    "✏️ Reply to this message with your custom answer "
                    "— your input box is locked to reply mode."
                )
                if prompt_id is not None and prompt_id not in accepted_ids:
                    accepted_ids.append(prompt_id)
                    prompt_ids.append(prompt_id)
                continue

            if action == "submit":
                labels = [options[i].get("label", "") for i in sorted(selected)]
                return "option", ", ".join(labels) if labels else "Other"

            idx = int(action) if action.isdigit() else -1
            if idx < 0 or idx >= len(options):
                continue

            if multi:
                if idx in selected:
                    selected.discard(idx)
                else:
                    selected.add(idx)
                buttons = _build_question_keyboard(options, multi, selected, show_more=more_shown)
                ch.edit_buttons(message_id, buttons)
                continue
            else:
                # Single select — look up label from options by index
                return "option", options[idx].get("label", f"Option {idx+1}")

        elif update["type"] == "text":
            text = update["text"]
            if text:
                return "text", text

    return "timeout", None


def _prop_text(value):
    """Flatten a Notion property value to display text. Tolerant of the flat
    simplified schema this hook was originally built against AND the nested
    Notion REST shape ({"type": "status", "status": {"name": "..."}}, rich
    text arrays, file objects, etc.) — the exact --permission-prompt-tool /
    MCP wire format isn't documented, so this degrades gracefully instead of
    assuming one shape."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_prop_text(v) for v in value]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        t = value.get("type")
        if isinstance(t, str) and t in value:
            return _prop_text(value[t])
        for key in ("plain_text", "name", "url"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        for key in ("external", "file"):
            if key in value:
                nested = _prop_text(value[key])
                if nested:
                    return nested
        for v in value.values():
            nested = _prop_text(v)
            if nested:
                return nested
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _extract_url(value):
    """Best-effort URL extraction from a property value — prioritizes an
    actual URL over a display name (unlike _prop_text), since callers use
    this specifically to find an image to render as a Telegram photo.
    Tolerant of a plain string, a list of file objects, or Notion REST's
    nested {"type": "files", "files": [{"external": {"url": ...}}]} shape."""
    if isinstance(value, str):
        return value if value.startswith("http") else None
    if isinstance(value, list):
        for v in value:
            u = _extract_url(v)
            if u:
                return u
        return None
    if isinstance(value, dict):
        if isinstance(value.get("url"), str):
            return value["url"]
        for key in ("external", "file"):
            if key in value:
                u = _extract_url(value[key])
                if u:
                    return u
        t = value.get("type")
        if isinstance(t, str) and t in value:
            return _extract_url(value[t])
        return None
    return None


def _get_ci(d, *keys):
    """Case-insensitive, multi-key lookup. Returns the first non-empty match
    or None. Property naming conventions vary across Notion MCP server
    versions/wire formats (e.g. "Image" vs "image" vs "Image URL")."""
    if not isinstance(d, dict):
        return None
    lower_map = {}
    for k, v in d.items():
        if isinstance(k, str):
            lower_map.setdefault(k.lower(), v)
    for key in keys:
        v = lower_map.get(key.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def _extract_properties(tool_input):
    """Best-effort properties-dict extraction, tolerant of several plausible
    Notion MCP payload shapes — the exact wire format for
    --permission-prompt-tool / MCP tool_input isn't publicly documented."""
    if not isinstance(tool_input, dict):
        return {}
    for key in ("properties", "property_values"):
        v = tool_input.get(key)
        if isinstance(v, dict) and v:
            return v
    for key in ("pages", "children"):
        v = tool_input.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            inner = _extract_properties(v[0])
            if inner:
                return inner
    page = tool_input.get("page")
    if isinstance(page, dict):
        inner = _extract_properties(page)
        if inner:
            return inner
    return {}


# Property names (lowercased) that look like a FandomFusionWear-style
# character brief. Used as a shape hint, not a hard schema — see
# classify_notion_write(). Generic on purpose: any project pairing a
# "create a titled record with a couple of descriptive fields" call through
# this plugin gets the same treatment.
_CHARACTER_KEY_HINTS = {"name", "title", "creature type", "region",
                        "personality traits", "type"}


def classify_notion_write(tool_name, tool_input):
    """Best-effort, project-agnostic classification of a Notion MCP write so
    it can get a richer approval message (and a Retry option) instead of the
    generic JSON dump. Detection is shape-based only — never tied to a
    specific page/database ID — so it holds across projects and degrades to
    None (generic handling) on any schema mismatch.

    Deliberately tolerant of multiple plausible property/content locations
    (see _extract_properties / _get_ci) because the exact
    --permission-prompt-tool wire format is undocumented upstream, and even
    the interactive-hook tool_input shape for a given Notion MCP server
    build isn't guaranteed to match what this was written against.

    Returns one of "character_proposal", "status_decision", "image_ready",
    "content_edit", or None."""
    if not isinstance(tool_input, dict) or not isinstance(tool_name, str):
        return None

    if tool_name.endswith("create-pages"):
        props = _extract_properties(tool_input) or tool_input
        hits = sum(1 for k in props
                  if isinstance(k, str) and k.lower() in _CHARACTER_KEY_HINTS)
        if hits >= 2:
            return "character_proposal"
        return None

    if tool_name.endswith("update-page"):
        props = _extract_properties(tool_input) or tool_input
        if _get_ci(props, "Image", "Cover", "Image URL", "Files") is not None:
            return "image_ready"
        if _get_ci(props, "Status") is not None:
            return "status_decision"
        if _get_ci(tool_input, "command", "content", "children", "markdown",
                   "body", "page_content") is not None:
            return "content_edit"
        return None

    return None


def _rationale_from_transcript(transcript_path, max_chars=3000):
    """Full text of the subagent's own immediately-preceding message, when a
    transcript is available. Every subagent is required (by project
    convention) to state its rationale before a gated write, so this is a
    better approval preview than the raw tool_input alone.

    Only available on the interactive hook path — the headless MCP
    permission-prompt server never gets a transcript_path, so it relies on
    tool_input fields alone via build_notion_decision_message()."""
    if not transcript_path:
        return ""
    # The rationale is a nice-to-have on the preview: a transcript that is
    # missing, truncated, or shaped unexpectedly must degrade to "no
    # rationale", never take down the approval gate itself. Failing here
    # used to crash the hook before it sent anything, which Claude Code
    # reads as an outright denial.
    try:
        msgs = extract_last_messages(transcript_path, max_messages=1, max_chars=None, full_scan=True)
        if not msgs or msgs[-1]["role"] != "assistant":
            return ""
        return smart_truncate(mask_secrets(msgs[-1]["text"]), max_chars)
    except Exception as e:
        _log(f"RATIONALE UNAVAILABLE (continuing without it): {e!r}")
        return ""


def build_notion_decision_message(kind, tool_input, rationale="", session_tag=""):
    """Render a readable approval preview (+ optional image URL) for a
    classified Notion write. Falls back gracefully — never raises — since a
    schema mismatch should degrade to a plainer message, not a crash."""
    tag = f" · <code>{html_escape(session_tag)}</code>" if session_tag else ""
    rationale_block = f"\n\n{html_escape(rationale)}" if rationale else ""

    if kind == "character_proposal":
        props = _extract_properties(tool_input) or tool_input
        name = _prop_text(_get_ci(props, "Name", "Title") or "a new character")
        details = "\n".join(
            f"  • <b>{html_escape(k)}:</b> {html_escape(_prop_text(v))}"
            for k, v in props.items()
            if isinstance(k, str) and k.lower() not in ("name", "title") and v)
        details_block = f"\n\n{details}" if details and not rationale else ""
        header = f"🐾 <b>New character proposed: {html_escape(name)}</b>{tag}"
        return header + rationale_block + details_block, None

    if kind == "status_decision":
        props = _extract_properties(tool_input) or tool_input
        status = _prop_text(_get_ci(props, "Status") or "?")
        header = f"📋 <b>Status change requested → {html_escape(status)}</b>{tag}"
        return header + rationale_block, None

    if kind == "image_ready":
        # Deliberately text-only, never a photo: this preview is built from
        # whatever URL is in the Notion `Image` property, which for this
        # project is Recraft's hosted *source* asset — often SVG, which
        # Telegram's sendPhoto rejects (it only accepts JPEG/PNG/GIF/WEBP).
        # Both the URL-fetch and download-and-reupload paths fail on that,
        # and previously did so silently. The real image preview already
        # happens earlier via pre_approve.py, which uploads the actual local
        # raster PNG directly — this gate just needs to confirm the write.
        props = _extract_properties(tool_input) or tool_input
        image_val = _get_ci(props, "Image", "Cover", "Image URL", "Files")
        image_url = _extract_url(image_val)
        status = _get_ci(props, "Status")
        status_line = f"\n\n<b>Status →</b> {html_escape(_prop_text(status))}" if status else ""
        link_line = f"\n\n<a href=\"{html_escape(image_url)}\">Image asset</a>" if image_url else ""
        header = f"🎨 <b>Character image ready</b>{tag}"
        text = header + rationale_block + status_line + link_line
        return text, None

    if kind == "content_edit":
        body = _get_ci(tool_input, "command", "content", "markdown", "children",
                       "body", "page_content")
        body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2)
        header = f"📖 <b>Lore update proposed</b>{tag}"
        body_block = (f"\n\n{html_escape(smart_truncate(body_text, 2500))}"
                      if body_text and not rationale else "")
        return header + rationale_block + body_block, None

    return None, None


def build_decision_buttons(show_more=True):
    """Approve / Retry / Cancel — used for classified Notion decision gates
    instead of the generic Allow/Always/Deny row. Retry maps to a Deny with a
    machine-readable RETRY: message, so the subagent can distinguish 'revise
    and re-attempt once' from an outright Cancel."""
    rows = [[
        {"text": "✅ Approve", "callback_data": "allow"},
        {"text": "🔁 Retry", "callback_data": "retry"},
        {"text": "❌ Cancel", "callback_data": "deny"},
    ]]
    if show_more:
        rows.append([{"text": "📖 Full context", "callback_data": "more"}])
    return rows


RETRY_MESSAGE = (
    "RETRY: Reviewer requested a revision via Telegram. Make one focused "
    "revision informed by this and re-attempt the same write as a real tool "
    "call right away — do not ask for approval in chat. Do not retry a "
    "second time if this is denied again this run."
)
CANCEL_MESSAGE = (
    "CANCEL: Reviewer rejected this via Telegram. Leave status/state exactly "
    "as it was and stop — do not retry."
)

_DECISION_SHORT_LABELS = {
    "character_proposal": "New character proposal",
    "status_decision": "Status change",
    "image_ready": "Character image",
    "content_edit": "Lore update",
}


def handle_notion_decision(ch, state, kind, tool_name, tool_input,
                           transcript_path, poll_start_size, session_tag, cfg):
    """Shared by the interactive hook (main(), below) and the headless MCP
    permission-prompt server: sends the classified rich preview (text or
    photo), polls for allow/retry/deny, and returns either:
      - a decision dict: {"behavior": "allow"} or {"behavior": "deny", "message": ...}
      - the string "local" (interactive-only: user responded in the terminal)
      - None (send failed — caller decides how to fail closed/open)
    """
    rationale = _rationale_from_transcript(transcript_path)
    text, image_url = build_notion_decision_message(kind, tool_input, rationale, session_tag)
    is_photo = bool(image_url)
    # Telegram's photo-caption limit (1024 chars) is much smaller than the
    # text-message limit (4096) — truncating to the text limit here would
    # leave the caption itself rejected with another 400 Bad Request.
    caption_limit = 1024 if is_photo else 4090
    text = smart_truncate(text, caption_limit, marker="\n\n<i>…truncated</i>")
    show_more = bool(transcript_path) and cfg.get("context_turns", 3) > 0
    buttons = build_decision_buttons(show_more=show_more)
    short_label = _DECISION_SHORT_LABELS.get(kind, tool_name)

    try:
        if is_photo:
            msg_id = ch.send_photo(image_url, caption=text, buttons=buttons)
        else:
            msg_id = ch.send_message(text, buttons=buttons)
        state["msg_id"] = msg_id
        state["is_photo"] = is_photo
        state["tool_display"] = short_label
        _log(f"SENT notion-decision({kind}) msg_id={msg_id} photo={is_photo}")
    except Exception as e:
        _log(f"SEND FAILED: {e}")
        return None

    def _on_more():
        _log("User clicked More")
        sent, total = send_full_context(ch, msg_id, transcript_path, cfg.get("context_turns", 3))
        if sent == total:
            if total == 0:
                _log("No full context to expand")
            ch.edit_buttons(msg_id, build_decision_buttons(show_more=False))
            return True
        return False

    answer = poll_callback(ch, msg_id, transcript_path, poll_start_size, on_more=_on_more)
    state["resolved"] = True

    if answer == "local":
        edit_message_resolved(ch, msg_id, "local", tool_name, short_label, is_photo=is_photo)
        return "local"
    if answer == "timeout":
        _log("Telegram timeout, no response")
        edit_message_resolved(ch, msg_id, "timeout", tool_name, short_label, is_photo=is_photo)
        return {"behavior": "deny", "message": "Approval request timed out"}
    if answer == "allow":
        edit_message_resolved(ch, msg_id, "allow", tool_name, short_label, is_photo=is_photo,
                              label_overrides={"allow": "Approved"})
        return {"behavior": "allow"}
    if answer == "retry":
        edit_message_resolved(ch, msg_id, "retry", tool_name, short_label, is_photo=is_photo)
        return {"behavior": "deny", "message": RETRY_MESSAGE}
    if answer == "deny":
        edit_message_resolved(ch, msg_id, "deny", tool_name, short_label, is_photo=is_photo,
                              label_overrides={"deny": "Cancelled"})
        return {"behavior": "deny", "message": CANCEL_MESSAGE}
    edit_message_resolved(ch, msg_id, "expired", tool_name, short_label, is_photo=is_photo)
    return {"behavior": "deny", "message": "Approval request expired"}


def build_approval_buttons(permission_suggestions=None, show_more=True):
    """Assemble inline-keyboard rows for an approval message. Separate
    helper so we can rebuild without More after the user taps it."""
    row = [{"text": "✅ Allow", "callback_data": "allow"}]
    if permission_suggestions:
        row.append({"text": "✅ Always", "callback_data": "always"})
    row.append({"text": "❌ Deny", "callback_data": "deny"})
    rows = [row]
    if show_more:
        rows.append([{"text": "📖 Full context", "callback_data": "more"}])
    return rows


def send_approval_message(ch, tool_name, tool_display, context_lines, permission_suggestions=None, session_tag="", show_more=True):
    context_text = format_context_block(context_lines)

    cmd_block = f"\n\n<pre>{html_escape(tool_display)}</pre>" if tool_display else ""
    tag = f" · <code>{html_escape(session_tag)}</code>" if session_tag else ""
    text = (
        f"🔔 <b>Permission Request: {html_escape(tool_name)}</b>{tag}"
        f"{cmd_block}"
        f"{context_text}"
    )
    text = smart_truncate(text, 4090, marker="\n\n<i>…truncated</i>")
    return ch.send_message(text, buttons=build_approval_buttons(permission_suggestions, show_more=show_more))



def poll_callback(ch, message_id, transcript_path="", poll_start_size=0,
                  on_more=None):
    """Poll channel for callback only (ignores text messages).
    Runs until response, local action, or POLL_TIMEOUT_SECONDS (3 days).

    on_more: optional callable invoked when the user taps "More". Called
    at most once — duplicate clicks arriving before the button is edited
    away are ignored so we don't send the full context twice."""
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    more_shown = True

    while time.monotonic() < deadline:
        if check_local_response(transcript_path, poll_start_size, threshold=100):
            return "local"

        update = ch.poll(message_id)
        if update and update["type"] == "callback":
            # Re-check to catch local activity that happened in parallel
            # with the channel callback.
            if check_local_response(transcript_path, poll_start_size, threshold=100):
                return "local"
            if update["data"] == "more":
                if on_more and more_shown:
                    # Flip first so rapid-duplicate callbacks drop, then
                    # restore on failure so the user can retry.
                    more_shown = False
                    if on_more() is False:
                        more_shown = True
                continue
            return update["data"]

        time.sleep(1)

    return "timeout"


def edit_message_resolved(ch, message_id, status, tool_name, tool_display,
                          is_photo=False, label_overrides=None):
    """Edit message after resolution — just icon + title + command, clean and short.

    is_photo: route the edit through edit_caption instead of edit_message —
    Telegram requires editMessageCaption for messages sent via sendPhoto.
    label_overrides: per-call label overrides (e.g. rich Notion decisions use
    "Approved"/"Cancelled" instead of the generic "Allowed"/"Denied")."""
    icons = {"allow": "✅", "always": "✅", "deny": "❌", "retry": "🔁",
             "timeout": "⏰", "local": "🖥", "expired": "💤"}
    labels = {"allow": "Allowed", "always": "Always allowed", "deny": "Denied",
              "retry": "Retry requested", "timeout": "Timeout",
              "local": "Handled locally", "expired": "Session ended"}
    if label_overrides:
        labels = {**labels, **label_overrides}
    icon = icons.get(status, "✅")
    label = labels.get(status, status)

    cmd_block = f"\n\n<pre>{html_escape(tool_display)}</pre>" if tool_display else ""
    text = (
        f"{icon} <b>{label}: {html_escape(tool_name)}</b>"
        f"{cmd_block}"
    )

    # buttons=[] clears the inline keyboard so users can't tap Allow/Deny
    # on an already-resolved message (callback would land in pending and
    # TTL-expire silently).
    if is_photo:
        ch.edit_caption(message_id, text, buttons=[])
    else:
        ch.edit_message(message_id, text, buttons=[])



# ---------------------------------------------------------------- hook response

def respond_allow(permission_suggestions=None):
    decision = {"behavior": "allow"}
    if permission_suggestions:
        decision["updatedPermissions"] = permission_suggestions
    json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
        "decision": decision}}, sys.stdout)
    sys.stdout.flush()


def respond_allow_with_input(updated_input):
    """Allow + pre-fill tool input (used for AskUserQuestion answers)."""
    json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow", "updatedInput": updated_input}}}, sys.stdout)
    sys.stdout.flush()

def respond_deny(message=""):
    json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny", "message": message}}}, sys.stdout)
    sys.stdout.flush()

# ---------------------------------------------------------------- main

# Populated by main() so the top-level crash guard can reach the channel and
# the tool name after an unhandled exception has unwound main()'s frame.
_LAST_STATE = {}


def _fail_loud(exc):
    """Turn an unhandled hook crash into a *distinguishable* denial.

    Claude Code treats a hook that exits non-zero without writing a decision
    as a plain permission denial — byte-identical, from the calling agent's
    point of view, to the reviewer tapping Cancel. That is exactly how a
    missing import in this file silently killed two production Notion writes
    on 2026-08-27: the agent reported "denied", the operator never saw a
    Telegram message, and nothing in the run said the gate had broken.

    So on a crash: log the traceback, alert the channel out-of-band, and emit
    an explicit deny whose message marks it as a system fault rather than a
    human decision."""
    _log(f"CRASH: {exc!r}\n{traceback.format_exc()}")

    state = _LAST_STATE.get("state") or {}
    ch = state.get("ch")
    tool_name = state.get("tool_name", "?")
    if ch is not None:
        try:
            ch.send_message(
                "🛑 <b>Approval system error</b>\n\n"
                f"<pre>{html_escape(f'{type(exc).__name__}: {exc}')}</pre>\n\n"
                f"The gated call <code>{html_escape(tool_name)}</code> was auto-denied "
                "because this hook crashed. It was <b>not</b> reviewed by anyone.")
        except Exception as send_exc:
            _log(f"CRASH ALERT SEND FAILED: {send_exc!r}")

    respond_deny(
        "APPROVAL-SYSTEM ERROR: the remote-approval hook crashed before this call "
        f"could be reviewed ({type(exc).__name__}: {exc}). This is NOT a reviewer "
        "decision and NOT a denial. Do not revise and retry. Stop this stage and "
        "report the approval system as broken.")


def main():

    # State shared with signal handler and atexit.
    # prompt_ids: transient ForceReply prompts from AskUserQuestion's "Other"
    # branch. Deleted on any exit path so TG users don't see a dangling
    # "Reply to this message" lock on an already-resolved request.
    state = {"ch": None, "msg_id": None, "tool_name": "", "tool_display": "",
             "resolved": False, "prompt_ids": [], "is_photo": False}
    _LAST_STATE["state"] = state

    def _cleanup_prompts():
        if not state["ch"]:
            return
        for pid in state["prompt_ids"]:
            try:
                state["ch"].delete_message(pid)
            except Exception:
                pass
        state["prompt_ids"] = []

    def on_signal(sig, frame):
        _log(f"SIGNAL received: {sig}")
        state["resolved"] = True
        if state["msg_id"] and state["ch"]:
            import signal as _sig
            status = "expired" if sig == getattr(_sig, "SIGPIPE", None) else "local"
            edit_message_resolved(
                state["ch"], state["msg_id"],
                status, state["tool_name"], state["tool_display"],
                is_photo=state.get("is_photo", False),
            )
        _cleanup_prompts()
        sys.exit(0)

    import atexit

    def on_exit():
        """When hook dies for ANY reason, clean up stale Telegram buttons."""
        if state["msg_id"] and state["ch"] and not state["resolved"]:
            edit_message_resolved(
                state["ch"], state["msg_id"],
                "expired", state["tool_name"], state["tool_display"],
                is_photo=state.get("is_photo", False),
            )
        _cleanup_prompts()

    atexit.register(on_exit)

    for name in ("SIGTERM", "SIGHUP", "SIGINT", "SIGPIPE"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, on_signal)
        except (OSError, ValueError):
            pass

    try:
        raw = sys.stdin.read()
        _log(f"START, stdin={len(raw)} bytes")
        event = json.loads(raw)
    except Exception as e:
        _log(f"STDIN ERROR: {e}")
        sys.exit(0)

    cfg = load_config()
    ch, ch_err = create_channel(cfg)
    if not ch:
        _log(f"Channel unavailable: {ch_err}")
        sys.exit(0)
    _log(f"CONFIG: channel={cfg['channel_type']}, escalation={cfg['escalation_seconds']}")

    tool_name = event.get("tool_name", "?")
    tool_input = event.get("tool_input", {})
    transcript_path = event.get("transcript_path", "")
    tool_display = format_tool_display(tool_name, tool_input, limit=cfg["tool_display_max_chars"])
    session_tag = common_session_tag(event)

    state["ch"] = ch
    state["tool_name"] = tool_name
    state["tool_display"] = tool_display

    context_lines = format_context_lines(transcript_path, max_turns=cfg["context_turns"], max_chars=cfg["context_max_chars"])

    # Record transcript size BEFORE sleep
    poll_start_size = 0
    if transcript_path:
        try:
            poll_start_size = os.path.getsize(transcript_path)
        except OSError:
            pass

    # Phase 1: Wait locally, checking for local response every 0.5s
    deadline = time.monotonic() + cfg["escalation_seconds"]
    while time.monotonic() < deadline:
        try:
            time.sleep(0.5)
        except SystemExit:
            sys.exit(0)
        if check_local_response(transcript_path, poll_start_size):
            _log("User responded locally during sleep, skipping Telegram")
            sys.exit(0)

    # Re-record transcript size AFTER sleep (fresh baseline for polling phase)
    if transcript_path:
        try:
            poll_start_size = os.path.getsize(transcript_path)
        except OSError:
            pass

    # Phase 2 & 3: depends on tool type
    _log(f"ESCALATING: {tool_name} / {tool_display[:50]}")

    notion_kind = classify_notion_write(tool_name, tool_input)
    _log(f"NOTION CLASSIFY: tool_name={tool_name!r} kind={notion_kind!r} "
        f"input_keys={sorted(tool_input.keys()) if isinstance(tool_input, dict) else type(tool_input).__name__}")

    if tool_name == "AskUserQuestion":
        try:
            msg_id, question_text, options, multi = build_ask_user_question_message(
                ch, tool_input, context_lines, session_tag=session_tag,
                show_more=bool(transcript_path) and cfg["context_turns"] > 0)
            state["msg_id"] = msg_id
            _log(f"SENT question msg_id={msg_id} multi={multi}")
        except Exception as e:
            _log(f"SEND FAILED: {e}")
            sys.exit(0)

        def _on_more_question(selected, multi_state):
            _log("User clicked More")
            sent, total = send_full_context(ch, msg_id, transcript_path, cfg["context_turns"])
            if sent == total:  # includes 0==0 — nothing to show, drop button
                if total == 0:
                    _log("No full context to expand")
                ch.edit_buttons(msg_id, _build_question_keyboard(
                    options, multi_state, selected, show_more=False))
                return True
            _log(f"Full context incomplete ({sent}/{total}); keeping button")
            return False

        answer_type, answer_value = poll_question_answer(
            ch, msg_id, options, multi, transcript_path, poll_start_size,
            prompt_ids=state["prompt_ids"], on_more=_on_more_question)

        state["resolved"] = True
        if answer_type == "local":
            edit_message_resolved(ch, msg_id, "local", tool_name, question_text or tool_display)
            _cleanup_prompts()
            sys.exit(0)
        elif answer_type == "timeout":
            _log("AskUserQuestion timeout, no response")
            edit_message_resolved(ch, msg_id, "timeout", tool_name, question_text or tool_display)
            _cleanup_prompts()
            sys.exit(0)
        elif answer_type in ("option", "text"):
            selected = answer_value or ""
            edit_message_resolved(ch, msg_id, "allow", tool_name, f"{question_text}: {selected}")
            updated_input = dict(tool_input)
            updated_input["answers"] = {question_text: selected}
            respond_allow_with_input(updated_input)
            _cleanup_prompts()
        else:
            edit_message_resolved(ch, msg_id, "expired", tool_name, question_text or tool_display)
            _cleanup_prompts()
            sys.exit(0)

    elif notion_kind:
        result = handle_notion_decision(ch, state, notion_kind, tool_name, tool_input,
                                        transcript_path, poll_start_size, session_tag, cfg)
        if result is None or result == "local":
            sys.exit(0)
        if result.get("behavior") == "allow":
            respond_allow()
        else:
            respond_deny(result.get("message", ""))

    else:
        permission_suggestions = event.get("permission_suggestions")
        try:
            msg_id = send_approval_message(
                ch, tool_name, tool_display, context_lines,
                permission_suggestions, session_tag=session_tag,
                show_more=True)
            state["msg_id"] = msg_id
            _log(f"SENT msg_id={msg_id}")
        except Exception as e:
            _log(f"SEND FAILED: {e}")
            sys.exit(0)

        def _on_more():
            _log("User clicked More")
            tool_sent, tool_total = send_full_tool(ch, msg_id, tool_name, tool_input)
            ctx_sent, ctx_total = send_full_context(ch, msg_id, transcript_path, cfg["context_turns"])
            sent, total = tool_sent + ctx_sent, tool_total + ctx_total
            if sent == total:  # includes 0==0 — nothing to show, drop button
                if total == 0:
                    _log("No full context to expand")
                ch.edit_buttons(msg_id, build_approval_buttons(
                    permission_suggestions, show_more=False))
                return True
            _log(f"Full context incomplete ({sent}/{total}); keeping button")
            return False

        answer = poll_callback(ch, msg_id, transcript_path, poll_start_size,
                               on_more=_on_more)

        state["resolved"] = True
        if answer == "local":
            edit_message_resolved(ch, msg_id, "local", tool_name, tool_display)
            sys.exit(0)
        elif answer == "timeout":
            _log("Telegram timeout, no response")
            edit_message_resolved(ch, msg_id, "timeout", tool_name, tool_display)
            sys.exit(0)
        elif answer == "allow":
            edit_message_resolved(ch, msg_id, "allow", tool_name, tool_display)
            respond_allow()
        elif answer == "always":
            edit_message_resolved(ch, msg_id, "always", tool_name, tool_display)
            respond_allow(permission_suggestions=permission_suggestions)
        elif answer == "deny":
            edit_message_resolved(ch, msg_id, "deny", tool_name, tool_display)
            respond_deny("User denied via Telegram")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _exc:
        _fail_loud(_exc)
        # Exit 0 deliberately: a non-zero exit makes Claude Code discard our
        # stdout and substitute its own generic denial, which is the silent
        # failure this guard exists to prevent.
        sys.exit(0)
