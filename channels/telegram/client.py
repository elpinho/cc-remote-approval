"""
Telegram channel implementation — Bot API via urllib.

All Telegram-specific logic lives here and in poll.py.
Hooks never import from this module directly — they use the Channel interface.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from utils.channel import Channel

# Telegram's sendPhoto only accepts JPEG/PNG/GIF/WEBP. Recraft (and other
# asset hosts this pipeline points at) often serve SVG, which fails both the
# URL-fetch and the raw download-and-upload path with an opaque 400. This
# proxy is format-agnostic on input and always returns a PNG, so routing the
# fallback download through it turns "unsupported format" into a non-issue
# without every caller needing to know or care what format a given URL is.
IMAGE_PROXY_URL = "https://pngit.foxsgrno1.workers.dev/?url="


def tg_request(token, method, data=None):
    """Call Telegram Bot API via urllib. Token stays in-process (not visible in ps)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_error_detail(e):
    """Telegram's error responses carry a useful "description" field in the
    JSON body (e.g. "Bad Request: failed to get HTTP URL content") — urllib's
    HTTPError.__str__() only gives the bare status line ("HTTP Error 400: Bad
    Request"), which is nearly useless for diagnosing *why*. Best-effort: the
    body can only be read once, so callers get this instead of raw access."""
    try:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body).get("description", body)
        except json.JSONDecodeError:
            return body
    except Exception:
        return str(e)


def _encode_multipart(fields, files):
    """Minimal stdlib multipart/form-data encoder — no third-party deps per
    this plugin's coding standards. `fields` is a str->str dict, `files` is
    name -> (filename, bytes, content_type)."""
    boundary = uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))
    for name, (filename, content, content_type) in files.items():
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode())
        lines.append(f"Content-Type: {content_type}".encode())
        lines.append(b"")
        lines.append(content)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


class TelegramChannel(Channel):
    """Telegram Bot API implementation."""

    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _send(self, method, data):
        """Defense-in-depth: chat-scoped API calls must only target the
        configured chat_id. Catches accidental cross-chat sends from
        future refactors or new Channel-level helpers.

        Does NOT swallow transport errors — HTTPError / URLError propagate
        so the caller can log the real cause. Fire-and-forget methods
        (edit/delete/notification) wrap their own try/except."""
        chat_id = data.get("chat_id")
        if chat_id is not None and str(chat_id) != str(self.chat_id):
            raise RuntimeError(
                f"outbound chat_id mismatch: "
                f"requested={chat_id!r}, configured={self.chat_id!r}"
            )
        return tg_request(self.bot_token, method, data)

    def send_message(self, text, buttons=None, parse_mode="HTML"):
        """Raises the underlying transport error on failure — the hook
        wrapping this call is the right layer to log and decide whether
        to exit. Swallowing here would mask HTTPError 401 / URLError as a
        downstream NoneType crash."""
        data = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        if buttons:
            data["reply_markup"] = {"inline_keyboard": buttons}
        result = self._send("sendMessage", data)
        return result["result"]["message_id"]

    def edit_message(self, msg_id, text, buttons=None, parse_mode="HTML"):
        """Edit a message. buttons=None keeps existing inline keyboard;
        buttons=[] explicitly removes it (pass this on resolve so users
        don't see stale clickable buttons on a message that's already
        been handled); buttons=[[...]] replaces with new keyboard."""
        data = {"chat_id": self.chat_id, "message_id": msg_id,
                "text": text, "parse_mode": parse_mode}
        if buttons is not None:
            data["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self._send("editMessageText", data)
        except Exception:
            pass

    def send_photo(self, photo_url, caption="", buttons=None, parse_mode="HTML"):
        """Send a photo with an optional caption and inline buttons.

        Tries Telegram's URL-based sendPhoto first (cheap — Telegram fetches
        the URL server-side). Many signed/proxied CDN URLs (Recraft's
        included) fail that server-side fetch with a bare "400 Bad Request"
        and no useful detail from urllib's HTTPError, even though the URL is
        perfectly fetchable from here. On that failure, falls back to
        downloading the image ourselves and uploading the bytes directly via
        multipart/form-data — our network path to the URL may work even when
        Telegram's does not.

        Same error-propagation contract as send_message: raises on failure
        of BOTH paths so the caller can log and decide what to do."""
        data = {"chat_id": self.chat_id, "photo": photo_url,
                "caption": caption, "parse_mode": parse_mode}
        if buttons:
            data["reply_markup"] = {"inline_keyboard": buttons}
        try:
            result = self._send("sendPhoto", data)
            return result["result"]["message_id"]
        except urllib.error.HTTPError as e:
            url_error = _http_error_detail(e)
            return self._send_photo_upload(photo_url, caption, buttons, parse_mode, url_error)

    def _send_photo_upload(self, photo_url, caption, buttons, parse_mode, url_error):
        """Fallback for send_photo: download the image ourselves, then
        upload the bytes to Telegram via multipart/form-data instead of
        asking Telegram to fetch the URL itself.

        Downloads via IMAGE_PROXY_URL rather than photo_url directly — the
        proxy detects the source format and always returns a PNG, so this
        one hop also fixes the "Telegram rejects the format" failure mode
        (e.g. SVG), not just the "Telegram can't reach this URL" one."""
        proxied_url = IMAGE_PROXY_URL + urllib.parse.quote(photo_url, safe="")
        try:
            req = urllib.request.Request(
                proxied_url, headers={"User-Agent": "Mozilla/5.0 (compatible; cc-remote-approval)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                photo_bytes = resp.read()
        except Exception as e:
            raise RuntimeError(
                f"sendPhoto by URL failed ({url_error}); "
                f"download-and-upload fallback also failed to fetch/convert the image: {e}") from e

        fields = {"chat_id": str(self.chat_id), "caption": caption, "parse_mode": parse_mode}
        if buttons:
            fields["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        files = {"photo": ("photo.png", photo_bytes, "application/octet-stream")}
        body, content_type = _encode_multipart(fields, files)

        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"sendPhoto by URL failed ({url_error}); "
                f"upload fallback also failed: {_http_error_detail(e)}") from e
        return result["result"]["message_id"]

    def edit_caption(self, msg_id, caption, buttons=None, parse_mode="HTML"):
        """Edit a photo message's caption/buttons. Fire-and-forget, same
        contract as edit_message — resolve-time edits shouldn't crash the
        caller."""
        data = {"chat_id": self.chat_id, "message_id": msg_id,
                "caption": caption, "parse_mode": parse_mode}
        if buttons is not None:
            data["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self._send("editMessageCaption", data)
        except Exception:
            pass

    def edit_buttons(self, msg_id, buttons):
        try:
            self._send("editMessageReplyMarkup", {
                "chat_id": self.chat_id, "message_id": msg_id,
                "reply_markup": {"inline_keyboard": buttons},
            })
        except Exception:
            pass

    def delete_message(self, msg_id):
        try:
            self._send("deleteMessage", {
                "chat_id": self.chat_id, "message_id": msg_id,
            })
        except Exception:
            pass

    def poll(self, msg_id):
        from channels.telegram.poll import poll_once
        return poll_once(self.bot_token, msg_id, self.chat_id, tg_request)

    def send_notification(self, text, parse_mode="HTML"):
        try:
            self._send("sendMessage", {
                "chat_id": self.chat_id, "text": text, "parse_mode": parse_mode})
        except Exception:
            pass

    def send_reply_prompt(self, msg_id, text, force_reply=True):
        """Send a prompt asking user to type a text reply.

        When force_reply=True, TG clients auto-lock the user's input box to
        'Reply to this message' mode — their next message gets
        reply_to_message_id set automatically, even from notification quick
        reply or Apple Watch where swipe-to-quote isn't available.

        Returns the new prompt's message_id so the caller can accept replies
        threaded under it (in addition to the original message)."""
        data = {"chat_id": self.chat_id, "text": text,
                "reply_to_message_id": msg_id}
        if force_reply:
            data["reply_markup"] = {
                "force_reply": True,
                "input_field_placeholder": "Type your answer...",
            }
        try:
            resp = self._send("sendMessage", data)
            return resp["result"]["message_id"]
        except Exception:
            return None

    def answer_callback(self, callback_id, text="✅"):
        # answer_callback uses callback_query_id, not chat_id — skip the
        # chat assertion and call tg_request directly.
        try:
            tg_request(self.bot_token, "answerCallbackQuery", {
                "callback_query_id": callback_id, "text": text})
        except Exception:
            pass

    def send_reply(self, reply_to_msg_id, text, parse_mode="HTML"):
        """Send a new message anchored as a reply to reply_to_msg_id.
        Swallows transport errors — caller is typically sending a batch
        of context messages where one failure shouldn't abort the rest."""
        try:
            result = self._send("sendMessage", {
                "chat_id": self.chat_id, "text": text, "parse_mode": parse_mode,
                "reply_to_message_id": reply_to_msg_id,
            })
            return result["result"]["message_id"]
        except Exception:
            return None
