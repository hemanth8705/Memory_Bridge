"""Caller identification.

The Flutter app is the primary identifier: it parses the filename AND looks the
number up in the device's contact list, then sends phone_number/contact_name
along with the upload. Everything here is the fallback for when the app could
not resolve it, plus the contact upsert used by the pipeline.
"""
import re
from datetime import datetime, timezone
from typing import Optional

# The device's own recorder format, checked first because it is unambiguous:
#   Vaibhav Singh @ CI(08700648603)_20260906131241.mp3
#   08920474604(08920474604)_20260906134028.mp3
# -> name before the bracket, number inside it, YYYYMMDDHHMMSS after the "_".
# When the caller is not in contacts the recorder repeats the number as the
# name, so a "name" that is really just the number is treated as no name.
_BRACKETED_RE = re.compile(
    r"^(?P<name>.*?)\((?P<number>\+?[\d\s\-]{6,17}\d)\)_(?P<stamp>\d{14})$"
)

# Same shape but without the trailing timestamp, for recorders that omit it.
_BRACKETED_NO_STAMP_RE = re.compile(
    r"^(?P<name>.*?)\((?P<number>\+?[\d\s\-]{6,17}\d)\)"
)

# Matches the number-ish run inside names like:
#   Call_+919876543210_20260801_103000.mp3
#   919876543210_20260801.mp3
#   Call recording +91 98765 43210.m4a
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{6,17}\d)")

# Date/time blobs that would otherwise look like phone numbers.
_DATESTAMP_RE = re.compile(r"^(19|20)\d{6}$|^\d{6}$|^(19|20)\d{2}[-_]?\d{2}[-_]?\d{2}$")

_NOISE_WORDS = {
    "call", "calls", "recording", "recordings", "rec", "audio", "voice", "memo",
    "incoming", "outgoing", "in", "out", "new", "phone", "auto", "cube", "acr",
}


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Reduce a number to a stable comparison key: the last 10 digits.

    Keeps +9198... and 098... and 98... matching each other, which is what
    actually happens across recorders and contact lists.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def _bracketed(filename: str):
    """Match the device recorder's name(number)_timestamp shape."""
    stem = filename.rsplit(".", 1)[0]
    return _BRACKETED_RE.match(stem) or _BRACKETED_NO_STAMP_RE.match(stem)


def extract_recorded_at(filename: str) -> Optional[datetime]:
    """The call's own timestamp, straight out of the filename.

    More trustworthy than the file's mtime, which resets whenever a recording
    is copied or synced. Naive local wall-clock in the filename, so it is read
    as UTC - close enough for relative "3 weeks ago" display, and consistent
    with how the rest of the pipeline stores times.
    """
    match = _BRACKETED_RE.match(filename.rsplit(".", 1)[0])
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group("stamp"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def extract_phone_number(filename: str) -> Optional[str]:
    """Pull a phone number out of a recording filename, if there is one."""
    stem = filename.rsplit(".", 1)[0]

    bracketed = _bracketed(filename)
    if bracketed:
        cleaned = re.sub(r"[\s\-]", "", bracketed.group("number"))
        bare = cleaned.lstrip("+")
        if 7 <= len(bare) <= 15:
            return cleaned

    for candidate in _PHONE_RE.findall(stem):
        cleaned = re.sub(r"[\s\-()]", "", candidate)
        bare = cleaned.lstrip("+")
        if _DATESTAMP_RE.match(bare):
            continue
        if len(bare) < 7 or len(bare) > 15:
            continue
        return cleaned
    return None


def guess_name(filename: str) -> Optional[str]:
    """Fallback for name-based filenames like 'Rahul_call_2026_08_01.mp3'."""
    bracketed = _bracketed(filename)
    if bracketed:
        # "Vaibhav Singh @ CI(0870...)" -> "Vaibhav Singh @ CI".
        # "08920474604(08920474604)"    -> not a name, just the number again.
        candidate = bracketed.group("name").strip(" _-")
        if candidate and normalize_phone(candidate) is None:
            return candidate
        return None

    stem = filename.rsplit(".", 1)[0]
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\d+", " ", stem)
    words = [w for w in stem.split() if w and w.lower() not in _NOISE_WORDS]
    if not words:
        return None
    name = " ".join(words[:3]).strip()
    # A number-based filename leaves only punctuation behind ("+919..." -> "+").
    # That is not a name.
    if not any(ch.isalpha() for ch in name):
        return None
    return name.title()


def identify(filename: str, phone_number: Optional[str] = None,
             contact_name: Optional[str] = None) -> dict:
    """Best-effort caller identity for a recording."""
    phone = phone_number or extract_phone_number(filename)
    name = contact_name or (None if phone_number else guess_name(filename))
    key = normalize_phone(phone)
    if not key and not name:
        return {"phone_number": None, "name": "Unknown caller", "key": None,
                "identified": False}
    return {
        "phone_number": phone,
        "name": name or phone or "Unknown caller",
        "key": key,
        "identified": True,
    }


async def upsert_contact(db, identity: dict) -> dict:
    """Find or create the contact row for an identified caller."""
    now = datetime.now(timezone.utc)
    key = identity.get("key")
    name = identity.get("name") or "Unknown caller"

    query = {"phone_key": key} if key else {"name": name, "phone_key": None}
    existing = await db.contacts.find_one(query)

    if existing:
        updates = {"last_seen_at": now}
        # A real contact name from the device beats a filename guess.
        if identity.get("name") and existing.get("name") in (None, "Unknown caller", existing.get("phone_number")):
            updates["name"] = identity["name"]
        if identity.get("phone_number") and not existing.get("phone_number"):
            updates["phone_number"] = identity["phone_number"]
        await db.contacts.update_one({"_id": existing["_id"]}, {"$set": updates})
        return {**existing, **updates}

    doc = {
        "name": name,
        "phone_number": identity.get("phone_number"),
        "phone_key": key,
        "created_at": now,
        "last_seen_at": now,
    }
    result = await db.contacts.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc
