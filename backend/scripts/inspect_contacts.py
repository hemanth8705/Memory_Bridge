"""Show how contacts/memories are actually keyed, to diagnose lookup misses."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_url, tz_aware=True)
    db = client[settings.mongodb_db]
    try:
        print("=== CONTACTS ===")
        contacts = await db.contacts.find().to_list(None)
        for c in contacts:
            cid = str(c["_id"])
            memories = await db.memories.count_documents({"contact_id": cid})
            recordings = await db.recordings.count_documents({"contact_id": cid})
            print(f"  {c.get('name')!r}")
            print(f"    contact_id  : {cid}")
            print(f"    phone_number: {c.get('phone_number')!r}")
            print(f"    phone_key   : {c.get('phone_key')!r}"
                  f"{'   <-- NO KEY: unreachable by phone lookup' if not c.get('phone_key') else ''}")
            print(f"    memories    : {memories}   recordings: {recordings}")

        print("\n=== MEMORIES: do they carry a phone key of their own? ===")
        total = await db.memories.count_documents({})
        with_key = await db.memories.count_documents({"phone_key": {"$ne": None}})
        print(f"  {with_key}/{total} memories carry phone_key")

        print("\n=== RECORDINGS ===")
        async for r in db.recordings.find().sort("uploaded_at", -1).limit(15):
            print(f"  {r.get('filename')!r}")
            print(f"    phone_number={r.get('phone_number')!r} "
                  f"contact_name={r.get('contact_name')!r} "
                  f"phone_key={r.get('phone_key')!r} status={r.get('status')!r}")

        print("\n=== DUPLICATE-CONTACT CHECK (same person, split records) ===")
        seen: dict = {}
        for c in contacts:
            name = (c.get("name") or "").strip().lower()
            seen.setdefault(name, []).append(c)
        for name, group in seen.items():
            if len(group) > 1:
                print(f"  {name!r} appears {len(group)} times: "
                      f"{[str(g['_id']) for g in group]}")
        keys: dict = {}
        for c in contacts:
            if c.get("phone_key"):
                keys.setdefault(c["phone_key"], []).append(c)
        for key, group in keys.items():
            if len(group) > 1:
                print(f"  phone_key {key!r} appears {len(group)} times")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
