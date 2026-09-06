"""Backfill MongoDB memories into the Actian VectorAI DB.

The pipeline indexes as it goes, but anything processed before the vector layer
existed is only in Mongo - and semantic retrieval silently falls back for those
contacts. Run this once to catch the index up.

  python scripts/reindex_vectordb.py              # index what is missing
  python scripts/reindex_vectordb.py --recreate   # drop the collection first
  python scripts/reindex_vectordb.py --contact Priya

--recreate is what you want after changing EMBEDDING_MODEL or EMBEDDING_DIM:
vectors of a different dimension, or from a different model, cannot be compared
against the new ones and the collection has to be rebuilt.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, vectordb  # noqa: E402
from app.config import get_settings  # noqa: E402

# One recording at a time: each is a separate embed + upsert round trip, and
# a whole backlog in one call would blow past Gemini's per-request batch limit.
BATCH_NOTE = "one recording per call"


async def main(recreate: bool, contact_filter: str | None) -> int:
    settings = get_settings()
    await db.connect()
    database = db.get_db()

    if recreate:
        # Delete before connect() re-creates it, so the new collection picks up
        # the current EMBEDDING_DIM.
        from actian_vectorai import AsyncVectorAIClient
        async with AsyncVectorAIClient(settings.vector_url,
                                       api_key=settings.vector_api_key or None) as client:
            if await client.collections.exists(settings.vector_collection):
                await client.collections.delete(settings.vector_collection)
                print(f"Dropped collection {settings.vector_collection!r}")

    await vectordb.connect()
    if not vectordb.is_available():
        print("VectorAI DB is not reachable. Is the container running?")
        print(f"  URL: {settings.vector_url}")
        print(f"  {(await vectordb.status()).get('error')}")
        await db.close()
        return 1

    print(f"Indexing into {settings.vector_collection!r} "
          f"({settings.embedding_model}, {settings.embedding_dim} dims)")

    contacts = {}
    async for doc in database.contacts.find():
        contacts[str(doc["_id"])] = doc.get("name") or "Unknown caller"

    query: dict = {"status": "completed"}
    if contact_filter:
        matches = [cid for cid, name in contacts.items()
                   if contact_filter.lower() in (name or "").lower()]
        if not matches:
            print(f"No contact matching {contact_filter!r}")
            await vectordb.close()
            await db.close()
            return 1
        query["contact_id"] = {"$in": matches}

    total_points = 0
    total_recordings = 0
    failures = 0

    async for recording in database.recordings.find(query):
        hash_ = recording.get("hash")
        contact_id = recording.get("contact_id")
        if not hash_:
            continue

        memories = []
        async for memory in database.memories.find({"source_recording": hash_}):
            memories.append(memory)

        report = await vectordb.index_memories(
            memories=memories,
            contact_id=contact_id,
            contact_name=contacts.get(contact_id, recording.get("contact_name") or ""),
            phone_key=recording.get("phone_key"),
            phone_number=recording.get("phone_number"),
            recorded_at=recording.get("recorded_at") or recording.get("processed_at"),
            summary=recording.get("summary", ""),
            topics=recording.get("topics", []),
            source_recording=hash_,
            source_transcript_id=recording.get("transcript_id"),
            api_key=None,  # falls back to GEMINI_API_KEY from .env
        )
        total_recordings += 1
        if report.get("error"):
            failures += 1
            print(f"  FAILED {recording.get('filename', hash_)}: {report['error']}")
        else:
            total_points += report["indexed"]
            print(f"  {contacts.get(contact_id, 'unknown'):<24} "
                  f"{report['indexed']:>3} points  {recording.get('filename', hash_)[:48]}")

    # Memories whose source recording is missing or was never marked completed
    # would be skipped by the loop above, so sweep them up by contact.
    orphan_query: dict = {"source_recording": {"$nin": [
        r.get("hash") async for r in database.recordings.find(query, {"hash": 1})
    ]}}
    if contact_filter:
        orphan_query["contact_id"] = query.get("contact_id")
    orphans: dict[str, list] = {}
    async for memory in database.memories.find(orphan_query):
        orphans.setdefault(memory.get("contact_id") or "", []).append(memory)

    for contact_id, rows in orphans.items():
        report = await vectordb.index_memories(
            memories=rows,
            contact_id=contact_id or None,
            contact_name=contacts.get(contact_id, ""),
            phone_key=(rows[0].get("phone_key") if rows else None),
            phone_number=(rows[0].get("phone_number") if rows else None),
            recorded_at=(rows[0].get("created_at") if rows else None),
            api_key=None,
        )
        if report.get("error"):
            failures += 1
            print(f"  FAILED orphan memories for {contacts.get(contact_id, contact_id)}: "
                  f"{report['error']}")
        elif report["indexed"]:
            total_points += report["indexed"]
            print(f"  {contacts.get(contact_id, 'unknown'):<24} "
                  f"{report['indexed']:>3} points  (memories with no completed recording)")

    stored = await vectordb.count_for()
    print(f"\n{total_recordings} recordings processed, {total_points} points written, "
          f"{failures} failures.")
    print(f"Collection now holds {stored} points.")

    await vectordb.close()
    await db.close()
    return 1 if failures and total_points == 0 else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recreate", action="store_true",
                        help="drop and rebuild the collection first")
    parser.add_argument("--contact", default=None,
                        help="only reindex contacts whose name contains this")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.recreate, args.contact)))
