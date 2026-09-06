"""Remove the throwaway data scripts/check_recorded_at.py creates.

That script tests against a fake contact (+910000000001 / "Timestamp
Regression Test") so it never touches real demo data - but it still writes to
whatever real MONGODB_URL is configured, so clean it up afterwards.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

TEST_NAME = "Timestamp Regression Test"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_url, tz_aware=True)
    db = client[settings.mongodb_db]
    try:
        contacts = await db.contacts.find({"name": TEST_NAME}).to_list(None)
        if not contacts:
            print("Nothing to clean up.")
            return
        contact_ids = [str(c["_id"]) for c in contacts]
        print(f"Removing {len(contact_ids)} test contact(s): {contact_ids}")

        r1 = await db.memories.delete_many({"contact_id": {"$in": contact_ids}})
        r2 = await db.recordings.delete_many({"contact_id": {"$in": contact_ids}})
        r3 = await db.transcripts.delete_many({"contact_id": {"$in": contact_ids}})
        r4 = await db.contacts.delete_many({"name": TEST_NAME})
        print(f"  memories:    {r1.deleted_count}")
        print(f"  recordings:  {r2.deleted_count}")
        print(f"  transcripts: {r3.deleted_count}")
        print(f"  contacts:    {r4.deleted_count}")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
