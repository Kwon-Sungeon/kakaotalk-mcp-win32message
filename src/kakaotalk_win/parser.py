"""Parse the text KakaoTalk PC puts on the clipboard for Ctrl+A / Ctrl+C in a chat.

Observed format (KakaoTalk PC 26.x, Korean UI):

    2026년 11월 10일 월요일\r\n
    [보낸사람] [오후 3:05] 첫 줄\n둘째 줄\r\n
    홍길동님이 들어왔습니다.\r\n

Records are separated by CRLF; line breaks inside one message are bare LF.
A record is a date divider, a ``[sender] [time] body`` message, or a line
without that header (system notices such as joins/leaves). English UI
headers (``[Name] [3:05 PM] text`` and ``Monday, November 10, 2026``) are
also recognized. Nothing is guessed: unknown records are kept verbatim.
"""

import hashlib
import re

_KO_DATE = re.compile(r"^(\d{4})년 (\d{1,2})월 (\d{1,2})일 [월화수목금토일]요일$")
_EN_MONTHS = {name: index for index, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], start=1)}
_EN_DATE = re.compile(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), ("
                      + "|".join(_EN_MONTHS) + r") (\d{1,2}), (\d{4})$")
_HEADER = re.compile(r"^\[(?P<sender>[^\r\n]+?)\] \[(?P<time>(?:오전|오후) ?\d{1,2}:\d{2}|\d{1,2}:\d{2} ?(?:AM|PM))\]"
                     r"(?: (?P<body>.*))?$", re.DOTALL)
_TIME = re.compile(r"^(?:(?P<ko>오전|오후) ?)?(?P<h>\d{1,2}):(?P<m>\d{2})(?: ?(?P<en>AM|PM))?$")

# Bodies the app uses instead of content for non-text messages (exact matches only).
_MEDIA = {
    "사진": "photo", "동영상": "video", "이모티콘": "emoticon", "음성메시지": "voice", "파일": "file",
    "Photo": "photo", "Video": "video", "Emoticon": "emoticon", "Voice Message": "voice", "File": "file",
}
_MEDIA_PATTERNS = [
    (re.compile(r"^사진 \d+장$"), "photo"),
    (re.compile(r"^\d+ Photos$"), "photo"),
    (re.compile(r"^파일: .+"), "file"),
    (re.compile(r"^File: .+"), "file"),
]
_DELETED = {"삭제된 메시지입니다.", "This message has been deleted."}
_SYSTEM = re.compile(r"(님이 들어왔습니다\.|님이 나갔습니다\.|님을 초대했습니다\.|님을 내보냈습니다\.|"
                     r"님이 .+님을 초대했습니다\.|채팅방 관리자가|joined this chatroom|left this chatroom|"
                     r"invited .+ to the chatroom)")


def to_24h(text):
    match = _TIME.match(text or "")
    if not match:
        return None
    hour, minute = int(match["h"]), int(match["m"])
    marker = match["ko"] or match["en"]
    if marker in ("오후", "PM") and hour != 12:
        hour += 12
    elif marker in ("오전", "AM") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_date(record):
    match = _KO_DATE.match(record)
    if match:
        year, month, day = map(int, match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    match = _EN_DATE.match(record)
    if match:
        return f"{int(match[3]):04d}-{_EN_MONTHS[match[1]]:02d}-{int(match[2]):02d}"
    return None


def classify_body(body):
    if body in _DELETED:
        return "deleted", None
    if body in _MEDIA:
        return "media", _MEDIA[body]
    for pattern, media in _MEDIA_PATTERNS:
        if pattern.match(body):
            return "media", media
    return "text", None


def parse(raw, self_name=None):
    """Return a list of records in chat order (oldest first).

    Each message: {index, kind, sender, timeText, time, date, text, mediaType?, fromSelf?}
    Each non-message line: {index, kind: "system"|"unclassified", text, date}
    Date dividers are folded into the ``date`` of following records.
    """
    items = []
    if not raw:
        return items
    current_date = None
    for record in raw.replace("\r\n", "\x1e").split("\x1e"):
        if not record.strip():
            continue
        date = parse_date(record.strip())
        if date:
            current_date = date
            continue
        header = _HEADER.match(record)
        if header:
            body = (header["body"] or "").replace("\r", "")
            kind, media = classify_body(body)
            item = {"index": len(items), "kind": kind, "sender": header["sender"], "timeText": header["time"],
                    "time": to_24h(header["time"]), "date": current_date, "text": body}
            if media:
                item["mediaType"] = media
            if self_name:
                item["fromSelf"] = header["sender"] == self_name
            items.append(item)
            continue
        text = record.replace("\r", "")
        items.append({"index": len(items), "kind": "system" if _SYSTEM.search(text) else "unclassified",
                      "text": text, "date": current_date})
    return items


def snapshot_id(raw):
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:24]


def fingerprint(item):
    """Stable identity of a message for before/after comparison (not globally unique)."""
    key = "\x1f".join(str(item.get(name) or "") for name in ("kind", "sender", "date", "time", "text"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
