"""Backfill phone_key onto existing data, and repair timestamp-as-number contacts.

Two things to fix in data written before the phone number became the primary
key:

  1. recordings / transcripts / memories carry no phone_key at all, so they
     were only reachable through their contact row.
  2. A greedy phone regex used to match the 14-digit timestamp at the end of a
     recorder filename, producing contacts literally named "20260906134028"
     that hold a real person's memories under a bogus key.

Run with --apply to write; without it, it only reports.

    .venv/Scripts/python.exe scripts/migrate_phone_keys.py
    .venv/Scripts/python.exe scripts/migrate_phone_keys.py --apply
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bson import ObjectId  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app import contacts as contacts_mod  # noqa: E402
from app.config import get_settings  # noqa: E402

APPLY = "--apply" in sys.argv


def log(message: str) -> None:
    print(("APPLY  " if APPLY else "DRYRUN ") + message)


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_url, tz_aware=True)
    db = client[settings.mongodb_db]
    try:
        # --- 1. repair contacts whose "number" is really a timestamp --------
        print("\n=== contacts keyed on a timestamp ===")
        async for contact in db.contacts.find({}):
            raw = contact.get("phone_number") or ""
            digits = "".join(ch for ch in str(raw) if ch.isdigit())
            if not contacts_mod.looks_like_timestamp(digits):
                continue

            contact_id = str(contact["_id"])
            # Recover the true number from the recordings filed under it.
            true_number = None
            async for rec in db.recordings.find({"contact_id": contact_id}):
                candidate = contacts_mod.extract_phone_number(rec.get("filename", ""))
                if candidate:
                    true_number = candidate
                    break

            true_key = contacts_mod.normalize_phone(true_number)
            log(f"contact {contact.get('name')!r} ({contact_id}) has timestamp "
                f"number {raw!r} -> real number {true_number!r} key {true_key!r}")
            if not true_key:
                log("  could not recover a real number; leaving it alone")
                continue

            existing = await db.contacts.find_one(
                {"phone_key": true_key, "_id": {"$ne": contact["_id"]}}
            )
            if APPLY:
                if existing is not None:
                    # Merge into the correct contact and drop the bogus one.
                    target = str(existing["_id"])
                    for collection in (db.memories, db.recordings, db.transcripts):
                        await collection.update_many(
                            {"contact_id": contact_id},
                            {"$set": {"contact_id": target,
                                      "phone_key": true_key,
                                      "phone_number": true_number}},
                        )
                    await db.contacts.delete_one({"_id": contact["_id"]})
                    log(f"  merged into existing contact {target}")
                else:
                    await db.contacts.update_one(
                        {"_id": contact["_id"]},
                        {"$set": {"phone_number": true_number,
                                  "phone_key": true_key,
                                  "name": true_number}},
                    )
                    for collection in (db.memories, db.recordings, db.transcripts):
                        await collection.update_many(
                            {"contact_id": contact_id},
                            {"$set": {"phone_key": true_key,
                                      "phone_number": true_number}},
                        )
                    log("  rekeyed in place")

        # --- 2. backfill phone_key everywhere else --------------------------
        print("\n=== backfilling phone_key from each document's contact ===")
        contacts = {str(c["_id"]): c async for c in db.contacts.find({})}
        for name, collection in (("memories", db.memories),
                                 ("recordings", db.recordings),
                                 ("transcripts", db.transcripts)):
            missing = await collection.count_documents(
                {"$or": [{"phone_key": None}, {"phone_key": {"$exists": False}}]}
            )
            print(f"  {name}: {missing} document(s) without phone_key")
            if not missing:
                continue
            updated = 0
            async for doc in collection.find(
                {"$or": [{"phone_key": None}, {"phone_key": {"$exists": False}}]}
            ):
                contact = contacts.get(doc.get("contact_id"))
                # Fall back to the filename when there is no usable contact row.
                key = contact.get("phone_key") if contact else None
                number = contact.get("phone_number") if contact else None
                if not key and doc.get("filename"):
                    number = contacts_mod.extract_phone_number(doc["filename"])
                    key = contacts_mod.normalize_phone(number)
                if not key:
                    continue
                if APPLY:
                    await collection.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"phone_key": key, "phone_number": number}},
                    )
                updated += 1
            log(f"  {name}: {updated} document(s) given a phone_key")

        # --- 3. report ------------------------------------------------------
        print("\n=== result ===")
        for name, collection in (("memories", db.memories),
                                 ("recordings", db.recordings),
                                 ("transcripts", db.transcripts)):
            total = await collection.count_documents({})
            keyed = await collection.count_documents({"phone_key": {"$nin": [None]}})
            print(f"  {name}: {keyed}/{total} carry phone_key")

        if not APPLY:
            print("\nDry run only. Re-run with --apply to write these changes.")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
