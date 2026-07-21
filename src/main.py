import base64
import hashlib
import html
import json
import re
import time
import traceback
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

try:
    from workers import File, FormData, Response, WorkerEntrypoint, fetch
except ImportError:
    File = FormData = fetch = None

    class Response:
        def __init__(self, body: str, status: int = 200):
            self.body = body
            self.status = status

        @classmethod
        def json(cls, body: Any):
            return cls(json.dumps(body), status=200)

    class WorkerEntrypoint:
        pass
from output_format import format_result

DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 3900
DESIGNED_CODE_LIMIT = 1800
DEFAULT_SPAM_WINDOW_SECONDS = 8
DEFAULT_DUPLICATE_WINDOW_SECONDS = 600
DEFAULT_BOT_LABEL = "@Decryptor2_bot"
STATS_KEY = "usage_stats:v1"
ENABLE_IMPORT_LINKS = True
ENABLE_FULL_DETAILS_TXT = False
SHOW_SENSITIVE_FIELDS = True
ENABLE_USAGE_STATS = False
SPARKLES = "\u2728"
PERSON_ICON = "\U0001f464"
STOPWATCH_ICON = "\u23f1"
DROP_OUTPUT_KEY_PARTS = ("lockedappconfig",)
NPV_EXTENSIONS = (".npv", ".npvt", ".npv.txt", ".npvt.txt")
LAST_USER_ACTION: dict[int, float] = {}
USAGE_STATS_MEMORY: Dict[str, Any] = {}
DUPLICATE_RESULT_CACHE: Dict[str, Dict[str, Any]] = {}

TITLE_BY_DECRYPTOR = {
    "Dark Tunnel": "DARK TUNNEL DECRYPTOR",
    "HTTP Injector": "HTTP INJECTOR DECRYPTOR",
    "HTTP Custom": "HTTP CUSTOM DECRYPTOR",
    "SSC Custom": "SSC CUSTOM DECRYPTOR",
}

DECRYPTORS = (
    {
        "name": "HTTP Custom",
        "module": "HTTPCUSTOM",
        "extensions": (".hc", ".hc.txt"),
    },
    {
        "name": "SSC Custom",
        "module": "SSCCUSTOM",
        "extensions": (".ssc", ".ssc.txt"),
    },
    {
        "name": "HTTP Injector",
        "module": "HTTPINJECTOR",
        "extensions": (".ehi", ".ehi.txt"),
    },
    {
        "name": "Dark Tunnel",
        "module": "DARKTUNNEL",
        "extensions": (".dark", ".dark.txt"),
    },
)


def env_text(env: Any, name: str, default: str = "") -> str:
    value = getattr(env, name, default)
    return "" if value is None else str(value)


def parse_allowed_users(raw_value: str) -> set[int]:
    allowed: set[int] = set()
    for part in raw_value.split(","):
        part = part.strip()
        if part.isdigit():
            allowed.add(int(part))
    return allowed


def parse_positive_int(raw_value: str, default: int) -> int:
    try:
        value = int(raw_value)
    except Exception:
        return default
    return value if value > 0 else default


def file_digest(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def duplicate_cache_key(sender_id: Any, chat_id: Any, digest: str) -> str:
    return f"{sender_id or 'unknown'}:{chat_id or 'unknown'}:{digest}"


def cleanup_duplicate_cache(now: float) -> None:
    for key, item in list(DUPLICATE_RESULT_CACHE.items()):
        expires_at = float(item.get("expires_at") or 0)
        if expires_at <= now:
            DUPLICATE_RESULT_CACHE.pop(key, None)


def recent_duplicate_record(
    sender_id: Any,
    chat_id: Any,
    digest: str,
    window_seconds: int = DEFAULT_DUPLICATE_WINDOW_SECONDS,
) -> Optional[Dict[str, Any]]:
    now = time.time()
    cleanup_duplicate_cache(now)
    key = duplicate_cache_key(sender_id, chat_id, digest)
    item = DUPLICATE_RESULT_CACHE.get(key)
    if not item:
        return None
    if now - float(item.get("seen_at") or 0) > window_seconds:
        DUPLICATE_RESULT_CACHE.pop(key, None)
        return None
    return item


def remember_successful_file(
    sender_id: Any,
    chat_id: Any,
    digest: str,
    file_name: str,
    decryptor_name: str,
    window_seconds: int = DEFAULT_DUPLICATE_WINDOW_SECONDS,
) -> None:
    now = time.time()
    cleanup_duplicate_cache(now)
    while len(DUPLICATE_RESULT_CACHE) > 200:
        DUPLICATE_RESULT_CACHE.pop(next(iter(DUPLICATE_RESULT_CACHE)))
    DUPLICATE_RESULT_CACHE[duplicate_cache_key(sender_id, chat_id, digest)] = {
        "file_name": file_name,
        "decryptor_name": decryptor_name,
        "seen_at": now,
        "expires_at": now + window_seconds,
    }


def duplicate_file_message(record: Dict[str, Any]) -> str:
    decryptor = str(record.get("decryptor_name") or "config")
    file_name = str(record.get("file_name") or "this file")
    return (
        "♻️ Duplicate file detected\n\n"
        f"You already unlocked this file recently.\n"
        f"File: {file_name}\n"
        f"Format: {decryptor}\n\n"
        "Please use the previous result and copy buttons."
    )


def chunk_text(text: str, size: int = TELEGRAM_TEXT_LIMIT) -> Iterable[str]:
    for start in range(0, len(text), size):
        yield text[start : start + size]


def chunk_for_html_code(text: str, max_escaped_size: int = DESIGNED_CODE_LIMIT) -> Iterable[str]:
    chunk: list[str] = []
    escaped_size = 0

    for character in text:
        character_size = len(html.escape(character, quote=False))
        if chunk and escaped_size + character_size > max_escaped_size:
            yield "".join(chunk)
            chunk = []
            escaped_size = 0

        chunk.append(character)
        escaped_size += character_size

    if chunk:
        yield "".join(chunk)


def display_result_chunk(text: str, prefer_second_chunk: bool = False) -> str:
    chunks = chunk_for_html_code(text or " ")
    first_chunk = next(chunks, " ")
    second_chunk = next(chunks, None)
    if prefer_second_chunk and second_chunk:
        return second_chunk
    return first_chunk


def has_more_detail_than_preview(text: str) -> bool:
    chunks = chunk_for_html_code(text or " ")
    next(chunks, None)
    return next(chunks, None) is not None


def safe_result_name(file_name: str, decryptor_name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name or "telegram_config")
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", decryptor_name.lower())
    return f"{base}.{label}.txt"


def matched_extension(file_name: str, extensions: Iterable[str]) -> bool:
    lower_name = (file_name or "").lower().strip()
    return any(lower_name.endswith(extension) for extension in extensions)


def is_npv_file(file_name: str) -> bool:
    return matched_extension(file_name, NPV_EXTENSIONS)


def decryptor_for_file(file_name: str) -> Optional[Dict[str, Any]]:
    for decryptor in DECRYPTORS:
        if matched_extension(file_name, decryptor["extensions"]):
            return decryptor
    return None


def ordered_decryptors(file_name: str) -> Tuple[Dict[str, Any], ...]:
    detected = decryptor_for_file(file_name)
    if detected:
        return (detected,)
    return DECRYPTORS


def decryptor_for_content(file_bytes: bytes) -> Optional[Dict[str, Any]]:
    text = file_bytes[:12000].decode("utf-8", errors="ignore").strip()
    if not text:
        return None

    lower_text = text.lower()
    if lower_text.startswith(("dark://", "dtunnel://")) or "encryptedlockedconfig" in lower_text:
        return next(item for item in DECRYPTORS if item["name"] == "Dark Tunnel")

    probe = text.split("://", 1)[1] if "://" in text else text
    try:
        if len(probe) % 4:
            probe += "=" * (4 - len(probe) % 4)
        decoded = json.loads(__import__("base64").b64decode(probe).decode("utf-8"))
        if isinstance(decoded, dict) and "encryptedLockedConfig" in decoded:
            return next(item for item in DECRYPTORS if item["name"] == "Dark Tunnel")
    except Exception:
        pass

    return None


def ordered_decryptors_for(file_bytes: bytes, file_name: str) -> Tuple[Dict[str, Any], ...]:
    detected = decryptor_for_file(file_name) or decryptor_for_content(file_bytes)
    if detected:
        return (detected,)
    return DECRYPTORS


def decryptor_title(decryptor_name: str) -> str:
    return TITLE_BY_DECRYPTOR.get(decryptor_name, f"{decryptor_name.upper()} DECRYPTOR")


def file_format_label(file_name: str, decryptor_name: str = "") -> str:
    decryptor = decryptor_for_file(file_name)
    if decryptor:
        return str(decryptor["name"])
    if is_npv_file(file_name):
        return "NPV/NPVT"
    return decryptor_name or "Unknown"


def blank_stats() -> Dict[str, Any]:
    return {
        "total": 0,
        "success": 0,
        "failed": 0,
        "daily": {},
        "by_format": {},
        "by_decryptor": {},
        "updated_at": 0,
    }


def local_day_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 6 * 60 * 60))


def increment_counter(container: Dict[str, Any], key: str, amount: int = 1) -> None:
    container[key] = int(container.get(key) or 0) + amount


def normalize_stats(stats: Any) -> Dict[str, Any]:
    if not isinstance(stats, dict):
        stats = blank_stats()
    normalized = blank_stats()
    normalized.update(stats)
    for key in ("daily", "by_format", "by_decryptor"):
        if not isinstance(normalized.get(key), dict):
            normalized[key] = {}
    return normalized


async def read_usage_stats(env: Any) -> Dict[str, Any]:
    namespace = getattr(env, "USAGE_STATS", None)
    if namespace is None:
        return normalize_stats(USAGE_STATS_MEMORY)

    try:
        raw = await namespace.get(STATS_KEY)
    except Exception as exc:
        print(f"stats read failed: {exc}")
        return blank_stats()

    if not raw:
        return blank_stats()

    try:
        return normalize_stats(json.loads(str(raw)))
    except Exception as exc:
        print(f"stats parse failed: {exc}")
        return blank_stats()


async def write_usage_stats(env: Any, stats: Dict[str, Any]) -> None:
    namespace = getattr(env, "USAGE_STATS", None)
    if namespace is None:
        USAGE_STATS_MEMORY.clear()
        USAGE_STATS_MEMORY.update(normalize_stats(stats))
        return

    try:
        await namespace.put(STATS_KEY, json.dumps(stats, ensure_ascii=False))
    except Exception as exc:
        print(f"stats write failed: {exc}")


async def record_usage(
    env: Any,
    file_name: str,
    success: bool,
    decryptor_name: str = "",
) -> None:
    if not ENABLE_USAGE_STATS:
        return

    stats = await read_usage_stats(env)
    status_key = "success" if success else "failed"
    today = local_day_key()
    daily = stats["daily"].setdefault(today, {"total": 0, "success": 0, "failed": 0})

    increment_counter(stats, "total")
    increment_counter(stats, status_key)
    increment_counter(daily, "total")
    increment_counter(daily, status_key)
    increment_counter(stats["by_format"], file_format_label(file_name, decryptor_name))
    if decryptor_name:
        increment_counter(stats["by_decryptor"], decryptor_name)

    stats["updated_at"] = int(time.time())
    await write_usage_stats(env, stats)


def top_counters(counters: Dict[str, Any], limit: int = 6) -> str:
    if not counters:
        return "No data yet"
    items = sorted(counters.items(), key=lambda item: int(item[1] or 0), reverse=True)[:limit]
    return "\n".join(f"- {html.escape(str(name))}: <b>{int(count or 0)}</b>" for name, count in items)


def stats_message(stats: Dict[str, Any]) -> str:
    stats = normalize_stats(stats)
    today = local_day_key()
    daily = normalize_stats({"daily": stats["daily"]})["daily"].get(
        today,
        {"total": 0, "success": 0, "failed": 0},
    )
    success = int(stats.get("success") or 0)
    total = int(stats.get("total") or 0)
    failed = int(stats.get("failed") or 0)
    success_rate = int((success / total) * 100) if total else 0

    return (
        "<b>📊 USAGE STATS</b>\n\n"
        f"Total files : <b>{total}</b>\n"
        f"Success     : <b>{success}</b>\n"
        f"Failed      : <b>{failed}</b>\n"
        f"Success rate: <b>{success_rate}%</b>\n\n"
        f"<b>Today ({html.escape(today)})</b>\n"
        f"Files       : <b>{int(daily.get('total') or 0)}</b>\n"
        f"Success     : <b>{int(daily.get('success') or 0)}</b>\n"
        f"Failed      : <b>{int(daily.get('failed') or 0)}</b>\n\n"
        "<b>By Format</b>\n"
        f"{top_counters(stats['by_format'])}\n\n"
        "<b>By Decryptor</b>\n"
        f"{top_counters(stats['by_decryptor'])}"
    )


def result_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Supported", "callback_data": "supported"},
                {"text": "Owner", "url": "https://t.me/Foridul_002"},
            ],
        ]
    }


def start_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Owner", "url": "https://t.me/Foridul_002"}],
            [{"text": "Supported Formats", "callback_data": "supported"}],
        ]
    }


def should_drop_output_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
    return any(part in normalized for part in DROP_OUTPUT_KEY_PARTS)


def clean_output_key(key: Any) -> Any:
    if not isinstance(key, str):
        return key

    return re.sub(r"^Encrypted", "", key, flags=re.IGNORECASE)


def clean_output_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if should_drop_output_key(key):
                continue
            cleaned_item = clean_output_value(item)
            if is_empty_output_value(cleaned_item):
                continue
            cleaned[clean_output_key(key)] = cleaned_item
        return cleaned

    if isinstance(value, list):
        return [
            cleaned_item
            for item in value
            if not is_empty_output_value(cleaned_item := clean_output_value(item))
        ]

    return value


def is_empty_output_value(value: Any) -> bool:
    if value is None or value is False:
        return True
    if value in ("", [], {}):
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "null", "none"}:
        return True
    return False


def parse_nested_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: parse_nested_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [parse_nested_json(item) for item in value]
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return value

    try:
        return parse_nested_json(json.loads(stripped))
    except Exception:
        return value


def clean_result_text(result: str) -> str:
    try:
        parsed = json.loads(result)
    except Exception:
        return result

    return json.dumps(clean_output_value(parse_nested_json(parsed)), indent=4, ensure_ascii=False)


def normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).lower())


def compact_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:700] + "..." if len(value) > 700 else value
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
        return text[:700] + "..." if len(text) > 700 else text
    return value


def collect_preview_fields(value: Any, found: Dict[str, Any]) -> None:
    key_aliases = {
        "app": "app",
        "name": "name",
        "configname": "name",
        "ps": "name",
        "note": "note",
        "notes": "note",
        "type": "type",
        "protocol": "protocol",
        "ip": "server",
        "address": "server",
        "add": "server",
        "server": "server",
        "serverhost": "server",
        "hostaddress": "server",
        "sshhost": "server",
        "sshserver": "server",
        "host": "host",
        "remotehost": "server",
        "remoteaddress": "server",
        "proxyhost": "proxy_host",
        "proxy": "proxy",
        "port": "port",
        "serverport": "port",
        "sshport": "port",
        "remoteport": "port",
        "proxyport": "proxy_port",
        "payload": "payload",
        "custompayload": "payload",
        "sni": "sni",
        "servernameindication": "sni",
        "uuid": "uuid",
        "id": "uuid",
        "sshfield": "ssh_info",
        "ssh": "ssh_info",
        "sshinfo": "ssh_info",
        "user": "username",
        "username": "username",
        "sshuser": "username",
        "sshusername": "username",
        "authuser": "username",
        "pass": "password",
        "passwd": "password",
        "password": "password",
        "sshpass": "password",
        "sshpassword": "password",
        "authpass": "password",
        "network": "network",
        "net": "network",
        "transportnetwork": "network",
        "path": "path",
        "security": "security",
        "tls": "security",
        "encryption": "encryption",
        "scy": "encryption",
        "config": "v2ray_config",
        "v2rayconfig": "v2ray_config",
        "v2rrawjson": "v2ray_config",
        "v2rayrawjson": "v2ray_config",
        "overwriteserverdata": "v2ray_config",
    }
    preferred_v2ray_keys = {"v2rayconfig", "v2rrawjson", "v2rayrawjson", "overwriteserverdata"}

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = normalized_key(key)
            alias = key_aliases.get(normalized)
            if alias and alias not in found and not is_empty_output_value(item):
                found[alias] = parse_nested_json(item) if alias == "v2ray_config" else compact_value(item)
            elif (
                alias == "v2ray_config"
                and normalized in preferred_v2ray_keys
                and not is_empty_output_value(item)
            ):
                found[alias] = parse_nested_json(item)
            collect_preview_fields(item, found)
        return

    if isinstance(value, list):
        for item in value:
            collect_preview_fields(item, found)


def preview_fields_from_result(result: str, decryptor_name: str) -> Dict[str, Any]:
    try:
        parsed = parse_nested_json(json.loads(result))
    except Exception:
        return {"app": decryptor_name}

    found: Dict[str, Any] = {"app": decryptor_name}
    collect_preview_fields(parsed, found)
    return found


def clean_action_text(value: Any, max_len: int = 2400) -> str:
    if is_empty_output_value(value):
        return ""
    text = one_line_value(value, max_len).strip()
    return "" if text.lower() in {"false", "null", "none"} else text


def result_note_from_result(result: str, file_name: str, decryptor_name: str) -> str:
    found = preview_fields_from_result(result, decryptor_name)
    return clean_action_text(found.get("note")) or "Note not found"


def ssh_info_from_result(result: str, decryptor_name: str) -> str:
    found = preview_fields_from_result(result, decryptor_name)
    direct = clean_action_text(found.get("ssh_info"))
    if direct and "@" in direct and direct.count(":") >= 2:
        return direct

    server = clean_action_text(found.get("server") or found.get("host"))
    port = clean_action_text(found.get("port"), 60)
    username = clean_action_text(found.get("username"), 240)
    password = clean_action_text(found.get("password"), 240)
    if all((server, port, username, password)):
        return f"{server}:{port}@{username}:{password}"

    return direct


def payload_from_result(result: str, decryptor_name: str) -> str:
    return clean_action_text(preview_fields_from_result(result, decryptor_name).get("payload"))


def important_preview(
    result: str,
    decryptor_name: str,
    show_sensitive_fields: bool = SHOW_SENSITIVE_FIELDS,
) -> str:
    try:
        parsed = parse_nested_json(json.loads(result))
    except Exception:
        return json.dumps(
            {
                "app": decryptor_name,
                "status": "safe preview unavailable",
            },
            indent=4,
            ensure_ascii=False,
        )

    found = preview_fields_from_result(result, decryptor_name)

    if len(found) <= 1:
        return json.dumps(
            {
                "app": decryptor_name,
                "status": "safe preview unavailable",
            },
            indent=4,
            ensure_ascii=False,
        )

    ordered_keys = [
        "app",
        "type",
        "name",
        "protocol",
        "server",
        "host",
        "port",
        "proxy",
        "proxy_host",
        "proxy_port",
        "sni",
        "network",
        "path",
        "security",
        "encryption",
    ]
    if show_sensitive_fields:
        ordered_keys.extend(
            [
                "uuid",
                "username",
                "password",
                "payload",
            ]
        )
    preview = {key: found[key] for key in ordered_keys if key in found}
    return json.dumps(preview, indent=4, ensure_ascii=False)


def v2ray_links_from_result(result: str, file_name: str, decryptor_name: str) -> list[str]:
    try:
        parsed = parse_nested_json(json.loads(result))
    except Exception:
        return []

    found: Dict[str, Any] = {"app": decryptor_name}
    collect_preview_fields(parsed, found)
    links: list[str] = []
    name = v2ray_link_name(found, file_name)

    fallback_link = v2ray_link_from_fields(found, name)
    if fallback_link and fallback_link not in links:
        links.append(fallback_link)

    v2ray_config = found.get("v2ray_config")
    if not is_empty_output_value(v2ray_config):
        for entry in v2ray_server_entries(v2ray_config):
            link = v2ray_link_from_entry(entry, name)
            if link and link not in links:
                links.append(link)

    return links


def v2ray_link_from_entry(entry: Dict[str, Any], name: str) -> Optional[str]:
    protocol = str(entry.get("protocol") or "").strip().lower()
    if protocol == "vmess":
        return vmess_link(entry, name)
    if protocol == "trojan":
        return trojan_link(entry, name)
    if protocol == "vless":
        return vless_link(entry, name)
    return None


def v2ray_link_from_fields(found: Dict[str, Any], name: str) -> Optional[str]:
    protocol = str(found.get("type") or found.get("protocol") or "").strip().lower()
    if protocol not in {"vless", "vmess", "trojan"}:
        return None

    address = found.get("server")
    port = clean_port(found.get("port"))
    if is_empty_output_value(address) or not port:
        return None

    security = str(found.get("security") or "").strip().lower()
    tls = security if security in {"tls", "xtls", "reality"} else ""
    if not tls and (str(port) == "443" or not is_empty_output_value(found.get("sni"))):
        tls = "tls"

    entry = clean_record(
        {
            "protocol": protocol,
            "address": address,
            "server": address,
            "port": port,
            "id": found.get("uuid"),
            "uuid": found.get("uuid"),
            "password": found.get("password"),
            "security": found.get("security"),
            "encryption": found.get("encryption") or ("none" if protocol == "vless" else found.get("security")),
            "network": found.get("network"),
            "host": found.get("host"),
            "path": found.get("path"),
            "serverName": found.get("sni") or (found.get("host") if tls else None),
            "tls": tls,
        }
    )
    return v2ray_link_from_entry(entry, name)


def requester_name(sender: Dict[str, Any]) -> str:
    name = sender.get("username")
    if not name:
        parts = [sender.get("first_name", ""), sender.get("last_name", "")]
        name = " ".join(part for part in parts if part).strip()
    return (name or "USER").upper()


def requester_link(sender: Dict[str, Any]) -> str:
    name = html.escape(requester_name(sender))
    user_id = sender.get("id")
    if isinstance(user_id, int) or str(user_id).isdigit():
        return f'<a href="tg://user?id={int(user_id)}">{name}</a>'
    return f"<b>{name}</b>"


def plain_html_text(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


def one_line_value(value: Any, max_len: int = 700) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)

    text = text.replace("\r", "\\r").replace("\n", "\\n").strip()
    return text[: max_len - 3] + "..." if len(text) > max_len else text


def is_ip_address(value: Any) -> bool:
    text = str(value or "").strip()
    parts = text.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except Exception:
        return False


def readable_preview_value(key: str, value: Any) -> str:
    text = one_line_value(value)
    lower_text = text.lower()

    if key in {"type", "protocol"}:
        return text.upper()

    if key == "network":
        network_names = {
            "ws": "WebSocket (WS)",
            "websocket": "WebSocket (WS)",
            "tcp": "TCP",
            "grpc": "gRPC",
            "h2": "HTTP/2",
            "http": "HTTP",
            "kcp": "KCP",
            "quic": "QUIC",
        }
        return network_names.get(lower_text, text)

    if key == "security":
        security_names = {
            "tls": "TLS",
            "none": "None",
        }
        return security_names.get(lower_text, text)

    return text


def preview_object(preview: str, decryptor_name: str = "") -> Dict[str, Any]:
    try:
        parsed = json.loads(preview)
    except Exception:
        return {"app": decryptor_name, "details": preview}

    if isinstance(parsed, dict):
        if decryptor_name and "app" not in parsed:
            parsed["app"] = decryptor_name
        return parsed

    return {"app": decryptor_name, "details": parsed}


def result_quality_badges(data: Dict[str, Any], decryptor_name: str) -> list[str]:
    badges: list[str] = []

    def add(label: str) -> None:
        if label and label not in badges:
            badges.append(label)

    raw_type = " ".join(
        str(data.get(key) or "")
        for key in ("app", "type", "protocol", "network", "security", "encryption")
    ).lower()
    payload = str(data.get("payload") or "").lower()
    port = str(data.get("port") or "")

    if "ssh" in raw_type or data.get("username") or data.get("password"):
        add("SSH")
    if "vmess" in raw_type:
        add("VMESS")
    if "vless" in raw_type:
        add("VLESS")
    if "trojan" in raw_type:
        add("TROJAN")
    if "ws" in raw_type or "websocket" in raw_type or "upgrade: websocket" in payload:
        add("WS")
    if "tls" in raw_type or port == "443" or not is_empty_output_value(data.get("sni")):
        add("TLS")
    if not is_empty_output_value(data.get("proxy") or data.get("proxy_host")):
        add("Proxy OK")
    if not is_empty_output_value(data.get("payload")):
        add("Payload")
    if not badges and decryptor_name:
        add(decryptor_name.replace(" DECRYPTOR", "").title())

    return badges[:6]


def server_information(preview: str, decryptor_name: str) -> str:
    data = preview_object(preview, decryptor_name)
    lines: list[str] = []

    def add_line(icon: str, label: str, key: str, value: Any) -> None:
        if is_empty_output_value(value):
            return
        text = readable_preview_value(key, value)
        escaped = html.escape(text, quote=False)
        block_keys = {"server", "host", "proxy", "sni", "path", "uuid", "payload", "details"}
        if key in block_keys or len(text) > 34:
            lines.append(f"{icon} <b>{label}</b>\n<code>{escaped}</code>")
        else:
            lines.append(f"{icon} <b>{label}</b>  <code>{escaped}</code>")

    add_line("📦", "App", "app", data.get("app") or decryptor_name)
    add_line("📡", "Type", "type", data.get("type") or data.get("protocol"))
    add_line("📝", "Name", "name", data.get("name"))

    server_value = data.get("server")
    if not is_empty_output_value(server_value):
        add_line("🌍", "IP" if is_ip_address(server_value) else "Server", "server", server_value)

    add_line("☁️", "Host", "host", data.get("host"))
    add_line("🔌", "Port", "port", data.get("port"))

    proxy_value = data.get("proxy")
    proxy_host = data.get("proxy_host")
    proxy_port = data.get("proxy_port")
    if is_empty_output_value(proxy_value) and not is_empty_output_value(proxy_host):
        proxy_value = f"{one_line_value(proxy_host)}:{one_line_value(proxy_port)}" if not is_empty_output_value(proxy_port) else proxy_host
    add_line("🧩", "Proxy", "proxy", proxy_value)

    add_line("🛡", "SNI", "sni", data.get("sni"))
    add_line("🛰", "Network", "network", data.get("network"))
    add_line("📂", "Path", "path", data.get("path"))
    add_line("🔐", "Security", "security", data.get("security") or data.get("encryption"))
    add_line("🆔", "UUID", "uuid", data.get("uuid"))
    add_line("👥", "Username", "username", data.get("username"))
    add_line("🔑", "Password", "password", data.get("password"))
    add_line("📨", "Payload", "payload", data.get("payload"))

    details = data.get("details")
    if len(lines) <= 1 and not is_empty_output_value(details):
        add_line("📄", "Details", "details", details)

    return "\n".join(lines)


def first_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def clean_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in record.items() if not is_empty_output_value(value)}


def v2ray_stream_info(stream_settings: Any) -> Dict[str, Any]:
    stream = first_dict(stream_settings)
    if not stream:
        return {}

    info: Dict[str, Any] = {
        "network": stream.get("network"),
        "tls": stream.get("security"),
    }

    tls_settings = first_dict(stream.get("tlsSettings") or stream.get("xtlsSettings") or stream.get("realitySettings"))
    info.update(
        {
            "serverName": tls_settings.get("serverName"),
            "allowInsecure": tls_settings.get("allowInsecure"),
            "fingerprint": tls_settings.get("fingerprint"),
            "publicKey": tls_settings.get("publicKey"),
            "shortId": tls_settings.get("shortId"),
        }
    )

    ws_settings = first_dict(stream.get("wsSettings"))
    ws_headers = first_dict(ws_settings.get("headers"))
    info.update(
        {
            "host": ws_headers.get("Host") or ws_headers.get("host"),
            "path": ws_settings.get("path"),
        }
    )

    grpc_settings = first_dict(stream.get("grpcSettings"))
    info["serviceName"] = grpc_settings.get("serviceName")

    http_settings = first_dict(stream.get("httpSettings") or stream.get("h2Settings"))
    http_hosts = http_settings.get("host")
    if isinstance(http_hosts, list) and http_hosts:
        info["host"] = info.get("host") or http_hosts[0]
    elif isinstance(http_hosts, str):
        info["host"] = info.get("host") or http_hosts
    info["path"] = info.get("path") or http_settings.get("path")

    return clean_record(info)


def v2ray_server_entries(v2ray_config: Any) -> list[Dict[str, Any]]:
    parsed = parse_nested_json(v2ray_config)
    root = first_dict(parsed)
    config = first_dict(root.get("Config") or root.get("config")) or root
    outbounds = config.get("outbounds")
    if isinstance(outbounds, dict):
        outbounds = [outbounds]
    if not isinstance(outbounds, list):
        return []

    entries: list[Dict[str, Any]] = []
    for outbound in outbounds:
        if not isinstance(outbound, dict):
            continue

        protocol = outbound.get("protocol")
        if str(protocol).lower() in {"freedom", "blackhole", "dns"}:
            continue

        settings = first_dict(outbound.get("settings"))
        stream_info = v2ray_stream_info(outbound.get("streamSettings"))
        base = {"protocol": protocol}
        entries_before = len(entries)

        vnext = settings.get("vnext")
        if isinstance(vnext, dict):
            vnext = [vnext]
        if isinstance(vnext, list):
            for server in vnext:
                if not isinstance(server, dict):
                    continue
                users = server.get("users")
                if isinstance(users, dict):
                    users = [users]
                if not isinstance(users, list) or not users:
                    users = [{}]
                for user in users:
                    user_info = first_dict(user)
                    entries.append(
                        clean_record(
                            {
                                **base,
                                "address": server.get("address"),
                                "port": server.get("port"),
                                "id": user_info.get("id"),
                                "uuid": user_info.get("id"),
                                "alterId": user_info.get("alterId"),
                                "level": user_info.get("level"),
                                "security": user_info.get("security"),
                                "encryption": user_info.get("encryption"),
                                "flow": user_info.get("flow"),
                                **stream_info,
                            }
                        )
                    )

        servers = settings.get("servers")
        if isinstance(servers, dict):
            servers = [servers]
        if isinstance(servers, list):
            for server in servers:
                if not isinstance(server, dict):
                    continue
                entries.append(
                    clean_record(
                        {
                            **base,
                            "address": server.get("address"),
                            "server": server.get("server"),
                            "port": server.get("port"),
                            "id": server.get("id"),
                            "uuid": server.get("id"),
                            "password": server.get("password"),
                            "method": server.get("method"),
                            "email": server.get("email"),
                            "level": server.get("level"),
                            "security": server.get("security"),
                            **stream_info,
                        }
                    )
                )

        if len(entries) == entries_before:
            entries.append(clean_record({**base, **stream_info}))

    return entries


def v2ray_server_preview(v2ray_config: Any) -> Optional[Dict[str, Any]]:
    entries = v2ray_server_entries(v2ray_config)
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]
    return {"servers": entries}


def query_pairs(params: Dict[str, Any]) -> str:
    pairs = []
    for key, value in params.items():
        if is_empty_output_value(value):
            continue
        pairs.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
    return "&".join(pairs)


def clean_port(value: Any) -> str:
    return str(value or "").strip()


def v2ray_link_name(data: Dict[str, Any], file_name: str) -> str:
    return one_line_value(data.get("name") or file_name or "config", 80)


def tls_value(entry: Dict[str, Any]) -> str:
    value = str(entry.get("tls") or "").strip().lower()
    return "tls" if value in {"tls", "xtls", "reality"} else ""


def vmess_link(entry: Dict[str, Any], name: str) -> Optional[str]:
    address = entry.get("address") or entry.get("server")
    port = clean_port(entry.get("port"))
    user_id = entry.get("id") or entry.get("uuid")
    if is_empty_output_value(address) or not port or is_empty_output_value(user_id):
        return None

    payload = {
        "v": "2",
        "ps": name,
        "add": str(address),
        "port": port,
        "id": str(user_id),
        "aid": str(entry.get("alterId") or 0),
        "scy": str(entry.get("security") or entry.get("encryption") or "auto"),
        "net": str(entry.get("network") or "tcp"),
        "type": "none",
        "host": str(entry.get("host") or ""),
        "path": str(entry.get("path") or ""),
        "tls": tls_value(entry),
        "sni": str(entry.get("serverName") or ""),
    }
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()).decode()
    return f"vmess://{encoded}"


def trojan_link(entry: Dict[str, Any], name: str) -> Optional[str]:
    address = entry.get("address") or entry.get("server")
    port = clean_port(entry.get("port"))
    password = entry.get("password")
    if is_empty_output_value(address) or not port or is_empty_output_value(password):
        return None

    params = {
        "security": tls_value(entry) or "none",
        "type": entry.get("network"),
        "host": entry.get("host"),
        "path": entry.get("path"),
        "sni": entry.get("serverName"),
        "allowInsecure": "1" if entry.get("allowInsecure") is True else "",
    }
    query = query_pairs(params)
    suffix = f"?{query}" if query else ""
    return f"trojan://{quote(str(password), safe='')}@{address}:{port}{suffix}#{quote(name, safe='')}"


def vless_link(entry: Dict[str, Any], name: str) -> Optional[str]:
    address = entry.get("address") or entry.get("server")
    port = clean_port(entry.get("port"))
    user_id = entry.get("id") or entry.get("uuid")
    if is_empty_output_value(address) or not port or is_empty_output_value(user_id):
        return None

    params = {
        "encryption": entry.get("encryption") or "none",
        "security": tls_value(entry) or "none",
        "type": entry.get("network"),
        "host": entry.get("host"),
        "path": entry.get("path"),
        "sni": entry.get("serverName"),
        "flow": entry.get("flow"),
    }
    query = query_pairs(params)
    suffix = f"?{query}" if query else ""
    return f"vless://{quote(str(user_id), safe='')}@{address}:{port}{suffix}#{quote(name, safe='')}"


def v2ray_links_from_preview(preview: str, file_name: str, decryptor_name: str) -> list[str]:
    data = preview_object(preview, decryptor_name)
    links: list[str] = []
    name = v2ray_link_name(data, file_name)

    fallback_link = v2ray_link_from_fields(data, name)
    if fallback_link and fallback_link not in links:
        links.append(fallback_link)

    v2ray_config = data.get("v2ray_config")
    if not is_empty_output_value(v2ray_config):
        for entry in v2ray_server_entries(v2ray_config):
            link = v2ray_link_from_entry(entry, name)
            if link and link not in links:
                links.append(link)

    return links


def v2ray_links_message(links: list[str]) -> str:
    if len(links) == 1:
        return (
            "<b>🔗 COPY READY V2RAY LINK</b>\n\n"
            f"<code>{html.escape(links[0], quote=False)}</code>"
        )

    parts = ["<b>🔗 COPY READY V2RAY LINKS</b>"]
    for index, link in enumerate(links, 1):
        parts.append(f"\n<b>Link {index}</b>\n<code>{html.escape(link, quote=False)}</code>")
    return "\n".join(parts)


def ready_v2ray_links_section(links: Optional[list[str]]) -> str:
    if not links:
        return ""

    link = links[0]
    return (
        "\n\n"
        "🔗 <b>READY V2RAY LINK</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{html.escape(link, quote=False)}</code>"
    )


def config_json_preview(preview: str, decryptor_name: str) -> str:
    data = preview_object(preview, decryptor_name)
    v2ray_config = data.get("v2ray_config")
    if not is_empty_output_value(v2ray_config):
        server_preview = v2ray_server_preview(v2ray_config)
        return json.dumps(
            {"v2ray_config": server_preview or v2ray_config},
            indent=4,
            ensure_ascii=False,
        )

    return preview


def designed_message(
    title: str,
    requester: str,
    bot_label: str,
    elapsed_ms: int,
    preview: str,
    prefer_second_chunk: bool = False,
    ready_links: Optional[list[str]] = None,
) -> str:
    requester_text = plain_html_text(requester) or "USER"
    section_line = "━━━━━━━━━━━━━━━━━━━━"
    server_info = server_information(preview, title)
    data = preview_object(preview, title)
    badges = result_quality_badges(data, title)
    badge_line = ""
    if badges:
        badge_line = f"🏷 <b>Badges</b> <code>{html.escape(' • '.join(badges), quote=False)}</code>\n"
    return (
        "✅ <b>DECRYPT COMPLETED</b>\n"
        f"{section_line}\n"
        f"🔓 <b>{html.escape(title)}</b>\n"
        f"👤 <b>User</b>  <code>{html.escape(requester_text, quote=False)}</code>\n"
        f"🤖 <b>Bot</b>   <code>{html.escape(bot_label, quote=False)}</code>\n"
        f"⚡ <b>Time</b>  <code>{elapsed_ms} ms</code>\n"
        "📊 <b>Status</b> <code>SUCCESS</code>\n\n"
        f"{badge_line}"
        "🌐 <b>SERVER INFORMATION</b>\n"
        f"{section_line}\n\n"
        f"{server_info}"
        f"{ready_v2ray_links_section(ready_links)}"
        "\n\n"
        "⬇️ <b>QUICK COPY</b>"
    )


def help_text() -> str:
    return (
        "<b>Config Unlocker</b>\n\n"
        "Supported files:\n"
        "- Dark Tunnel: <code>.dark</code>\n"
        "- HTTP Injector: <code>.ehi</code>\n"
        "- HTTP Custom: <code>.hc</code>\n"
        "- SSC Custom: <code>.ssc</code>\n\n"
        "Commands:\n"
        "- <code>/id</code> - your Telegram ID\n"
        "- <code>/chatid</code> - current chat/group ID\n\n"
        "Full output is enabled for everyone who can use this bot."
    )


def fail_message(file_name: str, detected_name: Optional[str], errors: Tuple[str, ...]) -> str:
    def technical_details(limit: int) -> str:
        if not errors:
            return ""
        details = "\n".join(f"- {error}" for error in errors[-limit:])
        return f"\n\nTechnical details:\n{details}"

    def classify_failure() -> tuple[str, str]:
        joined = " ".join(errors).lower()
        if not errors:
            return "Unsupported format", "Send a supported config file: .dark, .ehi, .hc, .ssc"
        if any(word in joined for word in ("json", "decode", "utf", "base64", "padding", "truncated", "corrupt")):
            return "Corrupted or incomplete file", "Download/export the config again, then resend it."
        if any(word in joined for word in ("decrypt", "cipher", "mac check", "invalid", "key", "nonce")):
            return "Decrypt key failed or config is locked", "This version may use a new lock method."
        if any(word in joined for word in ("timeout", "too long", "memory", "argon")):
            return "Heavy file or timeout", "Try a smaller file or resend after a moment."
        if "format/version did not match" in joined:
            return "Wrong format or unsupported version", "Check the file extension or send the original config file."
        return "Unsupported/new config version", "Send another file or wait for this format to be added."

    if is_npv_file(file_name):
        return (
            "❌ Unlock failed\n\n"
            f"File: {file_name}\n"
            "Detected: NPV/NPVT\n"
            "Reason: NPV support is currently disabled.\n"
            "Next step: send a supported file: .dark, .ehi, .hc, .ssc"
        )

    reason, next_step = classify_failure()
    if detected_name:
        return (
            "❌ Unlock failed\n\n"
            f"File: {file_name}\n"
            f"Detected: {detected_name}\n"
            f"Reason: {reason}\n"
            f"Next step: {next_step}"
            f"{technical_details(2)}"
        )

    return (
        "❌ Unsupported file\n\n"
        f"File: {file_name}\n"
        f"Reason: {reason}\n"
        "Supported: Dark Tunnel, HTTP Injector, HTTP Custom, SSC Custom.\n"
        f"Next step: {next_step}"
        f"{technical_details(4)}"
    )


async def notify_admins(client: "TelegramClient", raw_admin_ids: str, text: str) -> None:
    for admin_id in parse_allowed_users(raw_admin_ids):
        try:
            await client.send_message(admin_id, text)
        except Exception as exc:
            print(f"admin notify failed: {exc}")


def find_document(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = message.get("document")
    if isinstance(value, dict) and value.get("file_id"):
        return value
    return None


def short_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        message = type(exc).__name__
    return message[:180]


def run_decryptors(file_bytes: bytes, file_name: str) -> Tuple[Optional[str], Optional[str], Tuple[str, ...], Optional[str]]:
    errors: list[str] = []
    detected = decryptor_for_file(file_name) or decryptor_for_content(file_bytes)

    for decryptor in ordered_decryptors_for(file_bytes, file_name):
        name = str(decryptor["name"])
        module_name = str(decryptor["module"])
        try:
            module = __import__(module_name)
            runner = getattr(module, "run")
            result = runner(file_bytes)
        except Exception as exc:
            result = None
            errors.append(f"{name}: {short_error(exc)}")
            print(f"{name} failed: {exc}")

        if result:
            return name, clean_result_text(str(result)), tuple(errors), detected["name"] if detected else None

        errors.append(f"{name}: format/version did not match")

    return None, None, tuple(errors), detected["name"] if detected else None


class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    async def call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = await fetch(
            f"{self.api_base}/{method}",
            method="POST",
            headers={"content-type": "application/json"},
            body=json.dumps(payload),
        )
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.json()

    async def send_message(
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

        return await self.call("sendMessage", payload)

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str = "",
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        await self.call("editMessageText", payload)

    async def safe_edit_message(
        self,
        chat_id: int,
        message_id: Optional[int],
        text: str,
        parse_mode: str = "",
    ) -> None:
        if not message_id:
            return
        try:
            await self.edit_message(chat_id, message_id, text, parse_mode)
        except Exception as exc:
            print(f"editMessage failed: {exc}")

    async def send_long_text(self, chat_id: int, text: str) -> None:
        for chunk in chunk_text(text):
            await self.send_message(chat_id, chunk)

    async def get_bot_label(self) -> str:
        try:
            result = await self.call("getMe", {})
            username = result.get("result", {}).get("username")
            if username:
                return f"@{username}"
        except Exception as exc:
            print(f"getMe failed: {exc}")

        return DEFAULT_BOT_LABEL

    async def send_designed_result(
        self,
        chat_id: int,
        title: str,
        requester: str,
        bot_label: str,
        elapsed_ms: int,
        result: str,
        preview: str,
        prefer_second_chunk: bool = False,
        reply_to_message_id: Optional[int] = None,
        ready_links: Optional[list[str]] = None,
    ) -> None:
        await self.send_message(
            chat_id,
            designed_message(
                title,
                requester,
                bot_label,
                elapsed_ms,
                preview,
                prefer_second_chunk,
                ready_links,
            ),
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
            reply_markup=result_keyboard(),
        )

    async def send_document_text(
        self,
        chat_id: int,
        file_name: str,
        text: str,
        caption: str,
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        form = FormData()
        form.append("chat_id", str(chat_id))
        form.append("caption", caption)
        if reply_to_message_id:
            form.append("reply_to_message_id", str(reply_to_message_id))
            form.append("allow_sending_without_reply", "true")
        form.append(
            "document",
            File([text], file_name, content_type="text/plain; charset=utf-8"),
        )
        response = await fetch(
            f"{self.api_base}/sendDocument",
            method="POST",
            body=form.js_object,
        )
        if response.status >= 400:
            raise RuntimeError(await response.text())

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None:
        await self.call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )

    async def get_file_bytes(self, file_id: str) -> bytes:
        file_info = await self.call("getFile", {"file_id": file_id})
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            raise ValueError("Telegram did not return a file path.")

        response = await fetch(f"{self.file_base}/{file_path}")
        if response.status >= 400:
            raise RuntimeError(await response.text())
        return await response.bytes()

    async def set_webhook(self, webhook_url: str, secret_token: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "url": webhook_url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        }
        if secret_token:
            payload["secret_token"] = secret_token
        return await self.call("setWebhook", payload)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            return await self._fetch(request)
        except Exception:
            print(traceback.format_exc())
            return Response("Internal error", status=500)

    async def _fetch(self, request):
        token = env_text(self.env, "BOT_TOKEN")
        if not token:
            return Response("BOT_TOKEN missing", status=500)

        parsed_url = urlparse(request.url)
        path = parsed_url.path.rstrip("/") or "/"

        if path == "/":
            return Response("Telegram config bot is running.")

        if path == "/register":
            return await self.register_webhook(request, parsed_url, token)

        if path != "/webhook":
            return Response("Not found", status=404)

        if request.method != "POST":
            return Response("Method not allowed", status=405)

        secret = env_text(self.env, "WEBHOOK_SECRET")
        if secret:
            incoming_secret = request.headers.get("x-telegram-bot-api-secret-token")
            if incoming_secret != secret:
                return Response("Forbidden", status=403)

        try:
            update = await request.json()
        except Exception:
            return Response("Bad request", status=400)

        await self.handle_update(token, update)
        return Response("ok")

    async def register_webhook(self, request, parsed_url, token: str):
        setup_secret = env_text(self.env, "SETUP_SECRET")
        params = parse_qs(parsed_url.query)
        supplied_key = params.get("key", [""])[0]

        if setup_secret and supplied_key != setup_secret:
            return Response("Forbidden", status=403)

        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        webhook_url = f"{origin}/webhook"
        result = await TelegramClient(token).set_webhook(
            webhook_url,
            env_text(self.env, "WEBHOOK_SECRET"),
        )
        return Response.json(result)

    async def handle_callback(self, token: str, callback_query: Dict[str, Any]) -> None:
        client = TelegramClient(token)
        query_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") if isinstance(message, dict) else {}
        chat_id = chat.get("id") if isinstance(chat, dict) else None

        if data == "supported":
            text = "Supported: .dark, .ehi, .hc, .ssc"
        elif data == "full_txt":
            text = "Full details TXT is disabled."
        elif data == "raw_json":
            text = "Raw JSON is disabled for safety. The bot will send a safe preview."
        else:
            text = "OK"

        if query_id:
            await client.answer_callback_query(query_id, text, show_alert=False)

        if chat_id and data == "supported":
            await client.send_message(int(chat_id), help_text(), parse_mode="HTML", reply_markup=start_keyboard())

    async def handle_update(self, token: str, update: Dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self.handle_callback(token, callback_query)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return

        client = TelegramClient(token)
        text = message.get("text") or ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        document = find_document(message)
        if not command and not document:
            return

        sender = message.get("from") or {}
        allowed_users = parse_allowed_users(env_text(self.env, "ALLOWED_USER_IDS"))
        if allowed_users and sender.get("id") not in allowed_users:
            await TelegramClient(token).send_message(
                int(chat_id),
                "Sorry, this bot is private.",
            )
            return

        if command == "/stats":
            await client.send_message(
                int(chat_id),
                "The stats feature is currently disabled.",
                parse_mode="HTML",
            )
            return

        if command == "/start" or command == "/help":
            await client.send_message(
                int(chat_id),
                help_text(),
                parse_mode="HTML",
                reply_markup=start_keyboard(),
            )
            return

        if not document:
            return

        sender_id = sender.get("id")
        if isinstance(sender_id, int):
            spam_window = parse_positive_int(
                env_text(self.env, "SPAM_WINDOW_SECONDS"),
                DEFAULT_SPAM_WINDOW_SECONDS,
            )
            now = time.time()
            last_seen = LAST_USER_ACTION.get(sender_id, 0)
            wait_for = int(spam_window - (now - last_seen))
            if wait_for > 0:
                await client.send_message(
                    int(chat_id),
                    f"Please wait {wait_for}s before sending the next file.",
                )
                return
            LAST_USER_ACTION[sender_id] = now

        file_name = document.get("file_name") or "telegram_config"
        max_file_size = int(env_text(self.env, "MAX_FILE_SIZE", str(DEFAULT_MAX_FILE_SIZE)))
        file_size = int(document.get("file_size") or 0)
        if file_size and file_size > max_file_size:
            await record_usage(self.env, file_name, False)
            await client.send_message(
                int(chat_id),
                f"This file is too large. Limit: {max_file_size // (1024 * 1024)} MB.",
            )
            return

        reply_to_message_id = message.get("message_id")

        if is_npv_file(file_name):
            await record_usage(self.env, file_name, False)
            await client.send_message(
                int(chat_id),
                fail_message(file_name, None, ()),
                reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
            )
            return

        detected = decryptor_for_file(file_name)
        processing_text = "Unlocking config..."
        if detected:
            processing_text = f"Unlocking {detected['name']} config..."

        processing_message_id: Optional[int] = None
        try:
            processing = await client.send_message(
                int(chat_id),
                processing_text,
                reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
            )
            processing_message_id = processing.get("result", {}).get("message_id")
        except Exception as exc:
            print(f"processing message failed: {exc}")

        try:
            file_bytes = await client.get_file_bytes(document["file_id"])
        except Exception as exc:
            message_text = f"Could not download this file: {exc}"
            await record_usage(self.env, file_name, False)
            await client.safe_edit_message(int(chat_id), processing_message_id, message_text)
            if not processing_message_id:
                await client.send_message(int(chat_id), message_text)
            return

        digest = file_digest(file_bytes)
        duplicate_window = parse_positive_int(
            env_text(self.env, "DUPLICATE_WINDOW_SECONDS"),
            DEFAULT_DUPLICATE_WINDOW_SECONDS,
        )
        duplicate = recent_duplicate_record(sender_id, chat_id, digest, duplicate_window)
        if duplicate:
            message_text = duplicate_file_message(duplicate)
            await client.safe_edit_message(int(chat_id), processing_message_id, message_text)
            if not processing_message_id:
                await client.send_message(int(chat_id), message_text)
            return

        started_at = time.perf_counter()
        decryptor_name, result, errors, detected_name = run_decryptors(file_bytes, file_name)
        elapsed_ms = max(1, int((time.perf_counter() - started_at) * 1000))
        if not result or not decryptor_name:
            message_text = fail_message(file_name, detected_name, errors)
            await record_usage(self.env, file_name, False, detected_name or "")
            await client.safe_edit_message(int(chat_id), processing_message_id, message_text)
            if not processing_message_id:
                await client.send_message(
                    int(chat_id),
                    message_text,
                    reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
                )
            await notify_admins(
                client,
                env_text(self.env, "ADMIN_USER_IDS"),
                f"Unlock failed\nFile: {file_name}\nChat: {chat_id}\nErrors:\n" + "\n".join(errors[-6:]),
            )
            return

        result_name = safe_result_name(file_name, decryptor_name)
        title = decryptor_title(decryptor_name)
        requester = requester_link(sender)
        bot_label = await client.get_bot_label()
        caption = f"DONE | {decryptor_name}"
        preview = important_preview(result, decryptor_name)
        v2ray_links = v2ray_links_from_preview(preview, file_name, decryptor_name) if ENABLE_IMPORT_LINKS else []
        await record_usage(self.env, file_name, True, decryptor_name)

        try:
            await client.send_designed_result(
                int(chat_id),
                title,
                requester,
                bot_label,
                elapsed_ms,
                result,
                preview,
                decryptor_name == "HTTP Injector",
                int(reply_to_message_id) if reply_to_message_id else None,
                v2ray_links,
            )
        except Exception as exc:
            print(f"designed message failed, falling back to text chunks: {exc}")
            await client.send_message(
                int(chat_id),
                caption,
                reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
            )
            await client.send_message(
                int(chat_id),
                display_result_chunk(preview, decryptor_name == "HTTP Injector"),
            )
        remember_successful_file(
            sender_id,
            chat_id,
            digest,
            file_name,
            decryptor_name,
            duplicate_window,
        )
        await client.safe_edit_message(
            int(chat_id),
            processing_message_id,
            f"Done | {decryptor_name} | {elapsed_ms}ms",
        )
        if ENABLE_FULL_DETAILS_TXT:
            try:
                await client.send_document_text(
                    int(chat_id),
                    result_name,
                    format_result(title, result),
                    f"FULL DETAILS | {decryptor_name}",
                    int(reply_to_message_id) if reply_to_message_id else None,
                )
            except Exception as exc:
                print(f"full detail document failed: {exc}")
