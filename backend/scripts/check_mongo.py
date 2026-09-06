"""Standalone MongoDB connectivity + write-access check, reading MONGODB_URL from .env."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


async def main() -> None:
    settings = get_settings()
    if not settings.mongodb_url:
        print("MONGODB_URL is not set in .env")
        return
    client = AsyncIOMotorClient(
        settings.mongodb_url, serverSelectionTimeoutMS=10000, tz_aware=True,
    )
    try:
        await client.admin.command("ping")
        print("PING OK")
        db = client[settings.mongodb_db]
        names = await db.list_collection_names()
        print("database:", settings.mongodb_db)
        print("collections:", names)
        hc = db["_healthcheck"]
        result = await hc.insert_one({"probe": True})
        await hc.delete_one({"_id": result.inserted_id})
        print("WRITE OK")
    except Exception as exc:
        print("MONGO ERROR:", type(exc).__name__, exc)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
