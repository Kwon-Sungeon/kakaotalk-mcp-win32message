"""Account-safety guardrails, local state and send journals.

Kakao's published policies restrict accounts for: calling the protocol/API with
unofficial programs, reverse engineering/modifying the app, creating many chat
rooms or adding many friends in a short time, repeatedly sending the same
content / advertising, and accumulated spam reports. Detection is server-side,
based on usage environment and usage patterns. This module therefore enforces,
independently of whatever the AI agent decides:

* conservative send pacing (interval, per-hour, per-day, per-room caps) that
  can only be lowered by configuration, never raised;
* no broadcasting: the same text cannot go to another room within 24 hours;
* sends only into rooms that already have conversation history (never a new room);
* a circuit breaker that stops all sending after an app notice (e.g. a
  protection/restriction dialog) or repeated unconfirmed sends, until the user
  resets it;
* an optional native confirmation dialog before each send (``confirmSend: dialog``).

Pacing logs store only hashes. A send journal also keeps the room name (needed
to re-check an uncertain send); message bodies are only ever stored as hashes.
"""

import contextlib
import hashlib
import json
import msvcrt
import os
import re
import threading
import time
from pathlib import Path

from . import checks

STATE_ROOT = Path(os.environ.get("KAKAOTALK_WIN_STATE", Path(os.environ.get("LOCALAPPDATA", Path.home())) /
                                 "kakaotalk-win"))

# Hard ceilings. Configuration may lower these values but never raise them.
CEILINGS = {
    "min_interval_seconds": 10.0,   # minimum seconds between two sends (a floor, not a ceiling)
    "per_hour": 20,
    "per_day": 100,
    "per_room_per_hour": 10,
    "distinct_rooms_per_hour": 10,
    "duplicate_window_hours": 24.0,  # same text to a different room is blocked within this window
    "opens_per_minute": 6,
    "max_text_chars": 5000,
}

SHORT_REPLY_CHARS = 30
LINK = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

_thread_lock = threading.RLock()


def sha256_hex(value):
    return hashlib.sha256(bytes(value, "utf-8")).hexdigest()


def short_hash(value):
    return sha256_hex(value)[:16]


def normalize_text(text):
    """Whitespace-insensitive, case-insensitive form used for duplicate checks."""
    return " ".join(text.split()).casefold()


def _ensure_root():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return STATE_ROOT


def save_json(path, value):
    """Replace the file in one step, so a crash leaves either the old or the new content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        with open(part, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def load_json(path, default=None):
    try:
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    return json.loads(content)


_held = threading.local()


@contextlib.contextmanager
def process_lock(name="ui", wait_seconds=0.0):
    """Cross-process exclusive lock (msvcrt byte-range lock on a lock file); re-entrant per thread."""
    depth = getattr(_held, name, 0)
    if depth:
        setattr(_held, name, depth + 1)
        try:
            yield
        finally:
            setattr(_held, name, depth)
        return
    with _thread_lock:
        root = _ensure_root()
        handle = open(root / f".{name}.lock", "a+b")
        deadline = time.monotonic() + wait_seconds
        try:
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError("Another KakaoTalk operation is in progress") from None
                    time.sleep(0.05)
            setattr(_held, name, 1)
            try:
                yield
            finally:
                setattr(_held, name, 0)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


# --------------------------------------------------------------------------- config

def config_path():
    return STATE_ROOT / "config.json"


def load_config():
    value = load_json(config_path(), {}) or {}
    return value if isinstance(value, dict) else {}


def save_config(changes):
    current = load_config()
    current.update(changes)
    save_json(config_path(), current)
    return current


def limits():
    """Effective limits: configuration can only make them stricter."""
    configured = load_config().get("limits", {}) or {}
    effective = dict(CEILINGS)
    for key, ceiling in CEILINGS.items():
        value = configured.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if key == "min_interval_seconds" or key == "duplicate_window_hours":
            effective[key] = max(ceiling, float(value))
        else:
            effective[key] = max(0, min(ceiling, int(value)))
    return effective


def confirm_mode():
    """'off' (default): no extra dialog; the MCP client's per-call tool approval is the human check.

    'dialog': a native Yes/No box the human must answer before each send. An
    unrecognized value fails closed to 'dialog'.
    """
    value = os.environ.get("KAKAOTALK_WIN_CONFIRM") or load_config().get("confirmSend") or "off"
    return value if value in ("off", "dialog") else "dialog"


def self_name():
    return os.environ.get("KAKAOTALK_WIN_SELF_NAME") or load_config().get("selfName") or None


# --------------------------------------------------------------------------- pacing

def _send_log_path():
    return STATE_ROOT / "send-log.json"


def _recent_sends(now):
    entries = load_json(_send_log_path(), []) or []
    horizon = now - 86400 * 2
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("at", 0) >= horizon]


def check_send_allowed(room, text, now=None):
    """Return None when a send may proceed, else a blocked-result dict."""
    now = time.time() if now is None else now
    effective = limits()
    breaker = breaker_state()
    if breaker.get("tripped"):
        return {"status": "blocked", "reason": "safety_stop_active", "safetyStop": breaker,
                "detail": "Sending is stopped until the user checks KakaoTalk and runs `kakaotalk-win reset-stop`."}
    if len(text) > effective["max_text_chars"]:
        return {"status": "blocked", "reason": "text_too_long", "maxChars": effective["max_text_chars"]}
    sends = _recent_sends(now)
    room_key, text_key = short_hash(room), short_hash(normalize_text(text))
    if sends:
        wait = sends[-1]["at"] + effective["min_interval_seconds"] - now
        if wait > 0:
            return {"status": "blocked", "reason": "send_interval", "retryAfterSeconds": round(wait, 1)}
    hour = [entry for entry in sends if entry["at"] >= now - 3600]
    day = [entry for entry in sends if entry["at"] >= now - 86400]
    if len(hour) >= effective["per_hour"]:
        return {"status": "blocked", "reason": "hourly_send_limit", "limit": effective["per_hour"]}
    if len(day) >= effective["per_day"]:
        return {"status": "blocked", "reason": "daily_send_limit", "limit": effective["per_day"]}
    if sum(entry["room"] == room_key for entry in hour) >= effective["per_room_per_hour"]:
        return {"status": "blocked", "reason": "room_hourly_send_limit", "limit": effective["per_room_per_hour"]}
    rooms_this_hour = {entry["room"] for entry in hour}
    if room_key not in rooms_this_hour and len(rooms_this_hour) >= effective["distinct_rooms_per_hour"]:
        return {"status": "blocked", "reason": "too_many_rooms_per_hour",
                "limit": effective["distinct_rooms_per_hour"]}
    window = now - effective["duplicate_window_hours"] * 3600
    normalized = normalize_text(text)
    # Short everyday replies ("네 감사합니다") may go to several people; long or linked texts may not.
    broadcastable = len(normalized) > SHORT_REPLY_CHARS or LINK.search(normalized)
    if broadcastable and any(entry["text"] == text_key and entry["room"] != room_key and entry["at"] >= window
                             for entry in sends):
        return {"status": "blocked", "reason": "same_text_sent_to_another_room",
                "detail": "Broadcasting identical text to several rooms is not supported."}
    return None


def record_send(room, text, now=None):
    now = time.time() if now is None else now
    sends = _recent_sends(now)
    sends.append({"at": now, "room": short_hash(room), "text": short_hash(normalize_text(text))})
    save_json(_send_log_path(), sends)


def _open_log_path():
    return STATE_ROOT / "open-log.json"


def check_open_allowed(now=None):
    now = time.time() if now is None else now
    entries = [value for value in (load_json(_open_log_path(), []) or []) if isinstance(value, (int, float))]
    recent = [value for value in entries if value >= now - 60]
    if len(recent) >= limits()["opens_per_minute"]:
        return {"status": "blocked", "reason": "open_rate_limit", "limit": limits()["opens_per_minute"],
                "retryAfterSeconds": round(recent[0] + 60 - now, 1)}
    return None


def record_open(now=None):
    now = time.time() if now is None else now
    entries = [value for value in (load_json(_open_log_path(), []) or []) if isinstance(value, (int, float))]
    entries = [value for value in entries if value >= now - 3600] + [now]
    save_json(_open_log_path(), entries)


# --------------------------------------------------------------------------- circuit breaker

def _breaker_path():
    return STATE_ROOT / "safety-stop.json"


def breaker_state():
    return load_json(_breaker_path(), {}) or {}


def trip(reason, **detail):
    state = {"tripped": True, "reason": reason, "at": time.time(), **detail}
    save_json(_breaker_path(), state)
    return state


def reset_breaker():
    """Clear the stop and hand back what it was, so the CLI can show the reason that was cleared."""
    cleared = breaker_state()
    save_json(_breaker_path(), {"tripped": False, "resetAt": time.time()})
    return cleared


def note_unconfirmed_send():
    """Two unconfirmed sends in a row stop sending: something on screen needs a human."""
    path = STATE_ROOT / "unconfirmed.json"
    state = load_json(path, {"count": 0}) or {"count": 0}
    state["count"] = int(state.get("count", 0)) + 1
    save_json(path, state)
    if state["count"] >= 2:
        return trip("repeated_unconfirmed_sends")
    return None


def note_confirmed_send():
    save_json(STATE_ROOT / "unconfirmed.json", {"count": 0})


# Specific phrases of an actual measure. Generic words such as "운영정책" are left out on
# purpose: open chats show a standing policy banner that would otherwise stop everything.
RESTRICTION_KEYWORDS = ("보호조치", "이용이 제한", "이용제한 조치", "이용 제한 조치", "이용제한되", "계정이 제한",
                        "일시적으로 제한", "has been restricted", "has been suspended", "protection measure")


def mentions_restriction(text):
    lowered = (text or "").casefold()
    return any(keyword.casefold() in lowered for keyword in RESTRICTION_KEYWORDS)


# --------------------------------------------------------------------------- send journal

def journal_path(operation_id):
    """Journal file of one operation. The file name is a hash, so IDs never become paths."""
    name = sha256_hex(checks.operation_id(operation_id))
    return STATE_ROOT / "send-operations" / f"{name}.json"
