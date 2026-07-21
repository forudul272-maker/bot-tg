import html
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, abort, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from main import (  # noqa: E402
    DEFAULT_DUPLICATE_WINDOW_SECONDS,
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_SPAM_WINDOW_SECONDS,
    LAST_USER_ACTION,
    chunk_for_html_code,
    clean_action_text,
    decryptor_for_file,
    decryptor_title,
    designed_message,
    duplicate_file_message,
    fail_message,
    file_digest,
    find_document,
    help_text,
    important_preview,
    is_npv_file,
    payload_from_result,
    parse_allowed_users,
    parse_positive_int,
    preview_fields_from_result,
    recent_duplicate_record,
    remember_successful_file,
    requester_link,
    result_keyboard,
    result_note_from_result,
    run_decryptors,
    ssh_info_from_result,
    start_keyboard,
    v2ray_links_from_result,
)

app = Flask(__name__)
OWNER_BUTTON = {"text": "Owner", "url": "https://t.me/Foridul_002"}
COPY_CACHE: Dict[str, Dict[str, Any]] = {}


def env_text(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or ""


def bot_token() -> str:
    token = env_text("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN missing")
    return token


def env_bool(name: str, default: bool = False) -> bool:
    value = env_text(name)
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_value(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def is_owner(sender: Dict[str, Any]) -> bool:
    owner_ids = parse_allowed_users(env_text("OWNER_USER_IDS"))
    if not owner_ids:
        owner_ids = parse_allowed_users(env_text("ALLOWED_USER_IDS"))
    sender_id = sender.get("id")
    return isinstance(sender_id, int) and sender_id in owner_ids


def can_send_import_links(sender: Dict[str, Any], chat: Dict[str, Any]) -> bool:
    if not env_bool("ENABLE_IMPORT_LINKS", True):
        return False

    return is_allowed_private_output(sender, chat)


def can_send_sensitive_fields(sender: Dict[str, Any], chat: Dict[str, Any]) -> bool:
    if not env_bool("SHOW_SENSITIVE_FIELDS", True):
        return False

    return is_allowed_private_output(sender, chat)


def is_allowed_private_output(sender: Dict[str, Any], chat: Dict[str, Any]) -> bool:
    return True


def full_output_status_line(sender: Dict[str, Any], chat: Dict[str, Any]) -> str:
    chat_id = chat.get("id") or "unknown"
    chat_type = chat.get("type") or "unknown"
    enabled_status = "yes" if is_allowed_private_output(sender, chat) else "no"
    return (
        f"Chat ID: {chat_id}\n"
        f"Chat type: {chat_type}\n"
        f"Full output here: {enabled_status}\n"
        "Full output scope: everyone"
    )


def sender_label(sender: Dict[str, Any]) -> str:
    user_id = sender.get("id") or "unknown"
    username = sender.get("username")
    if username:
        return f"@{username} ({user_id})"

    parts = [str(sender.get("first_name") or ""), str(sender.get("last_name") or "")]
    name = " ".join(part for part in parts if part).strip()
    return f"{name or 'Unknown'} ({user_id})"


def chat_label(chat: Dict[str, Any]) -> str:
    chat_id = chat.get("id") or "unknown"
    chat_type = chat.get("type") or "unknown"
    title = chat.get("title") or chat.get("username") or ""
    suffix = f" | {title}" if title else ""
    return f"{chat_type} ({chat_id}){suffix}"


def short_log_errors(errors: tuple[str, ...]) -> str:
    if not errors:
        return "-"
    return "\n".join(f"- {html.escape(error[:180])}" for error in errors[-3:])


def send_owner_log(
    client: "TelegramClient",
    sender: Dict[str, Any],
    chat: Dict[str, Any],
    file_name: str,
    file_size: int,
    status: str,
    decryptor_name: str = "",
    elapsed_ms: Optional[int] = None,
    errors: tuple[str, ...] = (),
) -> None:
    owner_ids = parse_allowed_users(env_text("OWNER_USER_IDS"))
    if not owner_ids:
        owner_ids = parse_allowed_users(env_text("ALLOWED_USER_IDS"))
    if not owner_ids:
        return

    chat_id = int_value(chat.get("id"))
    file_mb = file_size / (1024 * 1024) if file_size else 0
    lines = [
        "<b>BOT LOG</b>",
        f"Status: <b>{html.escape(status)}</b>",
        f"User: {html.escape(sender_label(sender))}",
        f"Chat: {html.escape(chat_label(chat))}",
        f"File: <code>{html.escape(file_name)}</code>",
        f"Size: {file_mb:.2f} MB" if file_size else "Size: unknown",
    ]
    if decryptor_name:
        lines.append(f"Decryptor: <b>{html.escape(decryptor_name)}</b>")
    if elapsed_ms is not None:
        lines.append(f"Time: {elapsed_ms} ms")
    if errors:
        lines.append("Reason:")
        lines.append(short_log_errors(errors))

    text = "\n".join(lines)
    for owner_id in owner_ids:
        if chat.get("type") == "private" and chat_id == owner_id:
            continue
        try:
            client.send_message(owner_id, text, parse_mode="HTML")
        except Exception as exc:
            print(f"owner log failed: {exc}")


def remember_copy_text(title: str, text: str, chat_id: int) -> str:
    key = uuid.uuid4().hex[:16]
    COPY_CACHE[key] = {
        "title": title,
        "text": text,
        "chat_id": chat_id,
        "created_at": time.time(),
    }

    while len(COPY_CACHE) > 80:
        COPY_CACHE.pop(next(iter(COPY_CACHE)))

    return f"copy:{key}"


def copy_button(
    label: str,
    text: str,
    chat_id: int,
    send_long_text_as_message: bool = True,
) -> Dict[str, Any]:
    text = (text or "").strip()
    if text and len(text) <= 256:
        return {"text": label, "copy_text": {"text": text}}

    fallback = text or f"{label} not found"
    if not send_long_text_as_message:
        return {"text": label, "callback_data": "copy_direct_limit" if text else "copy_missing"}

    return {"text": label, "callback_data": remember_copy_text(label, fallback, chat_id)}


def action_result_keyboard(
    result: str,
    file_name: str,
    decryptor_name: str,
    sender: Dict[str, Any],
    chat: Dict[str, Any],
) -> Dict[str, Any]:
    chat_id = int_value(chat.get("id")) or 0
    rows: list[list[Dict[str, Any]]] = []

    if can_send_import_links(sender, chat):
        links = v2ray_links_from_result(result, file_name, decryptor_name)
        if links:
            rows.append([copy_button("Copy V2RAY", links[0], chat_id)])

    if can_send_sensitive_fields(sender, chat):
        fields = preview_fields_from_result(result, decryptor_name)
        ssh_info = ssh_info_from_result(result, decryptor_name)
        payload = payload_from_result(result, decryptor_name)
        sni = clean_action_text(fields.get("sni"), 240)
        uuid_text = clean_action_text(fields.get("uuid"), 240)

        if ssh_info:
            rows.append([copy_button("Copy SSH", ssh_info, chat_id)])
        if payload:
            rows.append([copy_button("Copy Payload", payload, chat_id)])

        short_fields = []
        if sni:
            short_fields.append(copy_button("Copy SNI", sni, chat_id))
        if uuid_text:
            short_fields.append(copy_button("Copy UUID", uuid_text, chat_id))
        if short_fields:
            rows.append(short_fields)

    if rows:
        rows.append([copy_button("Copy Note", result_note_from_result(result, file_name, decryptor_name), chat_id), OWNER_BUTTON])
        return {"inline_keyboard": rows}

    return result_keyboard()


def send_copy_text(client: "TelegramClient", chat_id: int, title: str, text: str) -> None:
    first = True
    for chunk in chunk_for_html_code(text or " "):
        heading = f"<b>{html.escape(title)}</b>\n\n" if first else ""
        client.send_message(
            chat_id,
            f"{heading}<code>{html.escape(chunk, quote=False)}</code>",
            parse_mode="HTML",
        )
        first = False


class TelegramClient:
    def __init__(self, token: str):
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    def call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(f"{self.api_base}/{method}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "",
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def edit_message(
        self,
        chat_id: int,
        message_id: Optional[int],
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not message_id:
            return
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            self.call("editMessageText", payload)
        except Exception as exc:
            print(f"editMessage failed: {exc}")

    def answer_callback_query(self, callback_query_id: str, text: str, show_alert: bool = False) -> None:
        self.call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )

    def get_bot_label(self) -> str:
        try:
            result = self.call("getMe", {})
            username = result.get("result", {}).get("username")
            if username:
                return f"@{username}"
        except Exception as exc:
            print(f"getMe failed: {exc}")
        return "@Decryptor2_bot"

    def get_file_bytes(self, file_id: str) -> bytes:
        file_info = self.call("getFile", {"file_id": file_id})
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            raise ValueError("Telegram did not return a file path.")
        response = requests.get(f"{self.file_base}/{file_path}", timeout=60)
        response.raise_for_status()
        return response.content

@app.get("/")
def health():
    return "Telegram config bot is running."


@app.get("/register")
def register_webhook():
    setup_secret = env_text("SETUP_SECRET")
    supplied_key = request.args.get("key", "")
    if setup_secret and supplied_key != setup_secret:
        abort(403)

    webhook_url = request.url_root.rstrip("/") + "/webhook"
    payload: Dict[str, Any] = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    secret = env_text("WEBHOOK_SECRET")
    if secret:
        payload["secret_token"] = secret
    return jsonify(TelegramClient(bot_token()).call("setWebhook", payload))


@app.post("/webhook")
def webhook():
    secret = env_text("WEBHOOK_SECRET")
    if secret and request.headers.get("x-telegram-bot-api-secret-token") != secret:
        abort(403)

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        abort(400)

    handle_update(update)
    return "ok"


def handle_callback(client: TelegramClient, callback_query: Dict[str, Any]) -> None:
    query_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    message = callback_query.get("message") or {}
    chat = message.get("chat") if isinstance(message, dict) else {}
    chat_id = chat.get("id") if isinstance(chat, dict) else None

    if data.startswith("copy:"):
        item = COPY_CACHE.get(data.split(":", 1)[1])
        cached_chat_id = int_value(item.get("chat_id")) if isinstance(item, dict) else None
        if item and chat_id and cached_chat_id == int(chat_id):
            if query_id:
                client.answer_callback_query(query_id, "Long text sent below.")
            send_copy_text(client, int(chat_id), str(item.get("title") or "Copy text"), str(item.get("text") or ""))
            return
        if query_id:
            client.answer_callback_query(query_id, "This button expired. Please send the file again.")
        return

    if data == "copy_direct_limit":
        if query_id:
            client.answer_callback_query(
                query_id,
                "Telegram direct copy supports up to 256 characters. This V2Ray link is too long.",
                show_alert=True,
            )
        return
    if data == "copy_missing":
        if query_id:
            client.answer_callback_query(query_id, "Nothing to copy.", show_alert=True)
        return

    if query_id:
        client.answer_callback_query(
            query_id,
            "Supported: .dark, .ehi, .hc, .ssc" if data == "supported" else "OK",
        )

    if chat_id and data == "supported":
        client.send_message(int(chat_id), help_text(), parse_mode="HTML", reply_markup=start_keyboard())


def handle_update(update: Dict[str, Any]) -> None:
    client = TelegramClient(bot_token())

    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        handle_callback(client, callback_query)
        return

    message = update.get("message")
    if not isinstance(message, dict):
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = message.get("text") or ""
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    document = find_document(message)
    if not command and not document:
        return

    sender = message.get("from") or {}
    allowed_users = parse_allowed_users(env_text("ALLOWED_USER_IDS"))
    if allowed_users and sender.get("id") not in allowed_users:
        client.send_message(int(chat_id), "Sorry, this bot is private.")
        return

    if command == "/start" or command == "/help":
        client.send_message(int(chat_id), help_text(), parse_mode="HTML", reply_markup=start_keyboard())
        return
    if command == "/id":
        user_id = sender.get("id") or "unknown"
        client.send_message(int(chat_id), f"Your Telegram ID: {user_id}")
        return
    if command == "/chatid":
        client.send_message(int(chat_id), full_output_status_line(sender, chat))
        return
    if command in {"/allowgroup", "/denygroup"}:
        client.send_message(int(chat_id), "Full output is already enabled for everyone.")
        return
    if command == "/fullstatus":
        client.send_message(int(chat_id), full_output_status_line(sender, chat))
        return
    if command == "/stats":
        client.send_message(int(chat_id), "The stats feature is currently disabled.")
        return

    if not document:
        return

    sender_id = sender.get("id")
    if isinstance(sender_id, int):
        spam_window = parse_positive_int(env_text("SPAM_WINDOW_SECONDS"), DEFAULT_SPAM_WINDOW_SECONDS)
        now = time.time()
        wait_for = int(spam_window - (now - LAST_USER_ACTION.get(sender_id, 0)))
        if wait_for > 0:
            client.send_message(int(chat_id), f"Please wait {wait_for}s before sending the next file.")
            return
        LAST_USER_ACTION[sender_id] = now

    file_name = document.get("file_name") or "telegram_config"
    file_size = int(document.get("file_size") or 0)
    max_file_size = int(env_text("MAX_FILE_SIZE", str(DEFAULT_MAX_FILE_SIZE)))
    reply_to_message_id = message.get("message_id")

    if file_size and file_size > max_file_size:
        send_owner_log(
            client,
            sender,
            chat,
            file_name,
            file_size,
            "REJECTED: TOO LARGE",
        )
        client.send_message(
            int(chat_id),
            f"This file is too large. Limit: {max_file_size // (1024 * 1024)} MB.",
            reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
            reply_markup=result_keyboard(),
        )
        return

    if is_npv_file(file_name):
        send_owner_log(
            client,
            sender,
            chat,
            file_name,
            file_size,
            "REJECTED: NPV DISABLED",
        )
        client.send_message(
            int(chat_id),
            fail_message(file_name, None, ()),
            reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
            reply_markup=result_keyboard(),
        )
        return

    detected = decryptor_for_file(file_name)
    processing = client.send_message(
        int(chat_id),
        f"Unlocking {detected['name']} config..." if detected else "Unlocking config...",
        reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
    )
    processing_message_id = processing.get("result", {}).get("message_id")

    try:
        file_bytes = client.get_file_bytes(document["file_id"])
        digest = file_digest(file_bytes)
        duplicate_window = parse_positive_int(
            env_text("DUPLICATE_WINDOW_SECONDS"),
            DEFAULT_DUPLICATE_WINDOW_SECONDS,
        )
        duplicate = recent_duplicate_record(sender.get("id"), chat_id, digest, duplicate_window)
        if duplicate:
            client.edit_message(
                int(chat_id),
                processing_message_id,
                duplicate_file_message(duplicate),
                reply_markup=result_keyboard(),
            )
            return
        started_at = time.perf_counter()
        decryptor_name, result, errors, detected_name = run_decryptors(file_bytes, file_name)
        elapsed_ms = max(1, int((time.perf_counter() - started_at) * 1000))
    except Exception as exc:
        send_owner_log(
            client,
            sender,
            chat,
            file_name,
            file_size,
            "ERROR",
            errors=(str(exc),),
        )
        client.edit_message(
            int(chat_id),
            processing_message_id,
            f"Could not process this file: {exc}",
            reply_markup=result_keyboard(),
        )
        return

    if not result or not decryptor_name:
        send_owner_log(
            client,
            sender,
            chat,
            file_name,
            file_size,
            "FAILED",
            detected_name or "",
            elapsed_ms,
            errors,
        )
        client.edit_message(
            int(chat_id),
            processing_message_id,
            fail_message(file_name, detected_name, errors),
            reply_markup=result_keyboard(),
        )
        return

    preview = important_preview(
        result,
        decryptor_name,
        can_send_sensitive_fields(sender, chat),
    )
    ready_links = v2ray_links_from_result(result, file_name, decryptor_name) if can_send_import_links(sender, chat) else []
    client.send_message(
        int(chat_id),
        designed_message(
            decryptor_title(decryptor_name),
            requester_link(sender),
            client.get_bot_label(),
            elapsed_ms,
            preview,
            decryptor_name == "HTTP Injector",
            ready_links,
        ),
        parse_mode="HTML",
        reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
        reply_markup=action_result_keyboard(result, file_name, decryptor_name, sender, chat),
    )
    remember_successful_file(
        sender.get("id"),
        chat_id,
        digest,
        file_name,
        decryptor_name,
        duplicate_window,
    )
    send_owner_log(
        client,
        sender,
        chat,
        file_name,
        file_size,
        "SUCCESS",
        decryptor_name,
        elapsed_ms,
    )
    client.edit_message(int(chat_id), processing_message_id, f"Done | {decryptor_name} | {elapsed_ms}ms")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
