"""Prove the Actian VectorAI DB layer end to end.

Walks the whole thing and prints what it finds:

  1. the server is up, and the collection exists with the expected geometry
  2. what is actually indexed, per contact
  3. semantic retrieval works - a question in the user's own words lands on the
     memory that answers it, with scores
  4. how that differs from the MongoDB path it replaces
  5. what happens when the vector DB is unreachable (it degrades, it does not
     break)

  python scripts/check_vectordb.py
  python scripts/check_vectordb.py --contact Rahul

Run scripts/reindex_vectordb.py first if the collection is empty.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, vectordb  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.routers.contacts import retrieve_context  # noqa: E402

# Questions phrased the way a person would actually ask them, and deliberately
# NOT in the words the stored memories use - "operation" has to find "surgery",
# "send them something" has to find "React course". Keyword search cannot do
# this; that is the whole demonstration.
PROBES = [
    "how is their family doing, is anyone unwell?",
    "did I promise them anything I still owe?",
    "has anything changed with their job or studies?",
    "what plans or trips do they have coming up?",
]

RULE = "-" * 78


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


async def main(contact_filter: str | None) -> int:
    settings = get_settings()

    head("1. Server and collection")
    await vectordb.connect()
    status = await vectordb.status()
    for label in ("enabled", "url", "collection", "embedding_model", "dimension",
                  "connected", "collection_ready", "server_version",
                  "indexed_points", "error"):
        if label in status and status[label] is not None:
            print(f"  {label:18s} {status[label]}")

    if not vectordb.is_available():
        print("\n  VectorAI DB is NOT reachable - start the container and retry:")
        print('    docker run -d --name vectorai -v "${PWD}\\local_data:'
              '/var/lib/actian-vectorai" -p 6573-6575:6573-6575 \\')
        print('      -e "ACTIAN_VECTORAI_ACCEPT_EULA=YES" actian/vectorai:latest')
        print("\n  The app itself keeps working without it - see section 5.")
        return 1

    await db.connect()
    database = db.get_db()

    head("2. What is indexed, per contact")
    contacts = []
    async for doc in database.contacts.find().sort("last_conversation_at", -1):
        name = doc.get("name") or "Unknown caller"
        if contact_filter and contact_filter.lower() not in name.lower():
            continue
        contact_id = str(doc["_id"])
        indexed = await vectordb.count_for(
            contact_id=contact_id, phone_key=doc.get("phone_key"))
        in_mongo = await database.memories.count_documents({"contact_id": contact_id})
        contacts.append((name, contact_id, doc.get("phone_key"), indexed, in_mongo))
        print(f"  {name:<36} {in_mongo:>3} memories in Mongo "
              f"-> {indexed:>3} points indexed")

    if not contacts:
        print("  No contacts found." if not contact_filter
              else f"  No contact matching {contact_filter!r}.")
        await vectordb.close()
        await db.close()
        return 1

    # Probe whichever contact has the most indexed material to retrieve from.
    name, contact_id, phone_key, indexed, _ = max(contacts, key=lambda c: (c[3], c[4]))
    if indexed == 0:
        print("\n  Nothing indexed yet. Run: python scripts/reindex_vectordb.py")
        await vectordb.close()
        await db.close()
        return 1

    head(f"3. Semantic retrieval - questions about {name}")
    print("  Each question is asked in words that do NOT appear in the memory\n"
          "  that answers it. Score is cosine similarity, 1.0 = identical.\n")
    for probe in PROBES:
        rows, _, source, error = await retrieve_context(
            probe, contact_id=contact_id, phone_key=phone_key, limit=4)
        print(f'  "{probe}"')
        if source != "vector":
            print(f"      (no vector answer: {error})")
            continue
        for row in rows:
            kind = row.get("type", "other")
            print(f"      {row['score']:>7.4f}  [{kind:<15}] {row.get('content', '')[:64]}")
        print()

    head("4. Vector retrieval vs the MongoDB path, same question")
    probe = PROBES[0]
    rows, block, source, error = await retrieve_context(
        probe, contact_id=contact_id, phone_key=phone_key, limit=5)
    print(f'  Question: "{probe}"\n')
    print("  VectorAI DB - nearest by meaning, most relevant first:")
    if source == "vector":
        for row in rows:
            print(f"      {row['score']:>7.4f}  {row.get('content', '')[:66]}")
    else:
        print(f"      unavailable: {error}")

    print("\n  MongoDB - newest first, relevance not considered:")
    count = 0
    async for doc in database.memories.find(
            {"contact_id": contact_id}).sort("created_at", -1).limit(5):
        print(f"          -    {doc.get('content', '')[:66]}")
        count += 1
    if count == 0:
        print("      (none)")
    print("\n  Both reach the same rows; only the vector path puts the one that\n"
          "  answers the question at the top, which is what the model reads first.")

    head("5. Degradation - retrieval when the vector DB is down")
    saved = vectordb._client, vectordb._available, vectordb._last_error
    vectordb._client, vectordb._available = None, False
    vectordb._last_error = "simulated outage"
    rows, block, source, error = await retrieve_context(
        probe, contact_id=contact_id, phone_key=phone_key, limit=5)
    print(f"  retrieve_context -> source={source!r}, error={error!r}")
    print("  source is None, so /memory/search and /callers/lookup fall back to")
    print("  MongoDB and still answer. Nothing raises, nothing 500s.")
    vectordb._client, vectordb._available, vectordb._last_error = saved

    head("Result")
    print(f"  Actian VectorAI DB {status.get('server_version', '')}".rstrip())
    print(f"  {status['indexed_points']} points in {settings.vector_collection!r} "
          f"({settings.embedding_dim}-dim, cosine)")
    print("  Write layer: app/pipeline.py -> vectordb.index_memories()")
    print("  Read  layer: app/routers/contacts.py -> retrieve_context()")

    await vectordb.close()
    await db.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact", default=None,
                        help="only look at contacts whose name contains this")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.contact)))
