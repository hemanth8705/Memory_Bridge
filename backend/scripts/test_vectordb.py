"""Checks for the Actian VectorAI DB layer.

Sections 1-4 are pure logic and need nothing running. Section 5 needs the
container up and a Gemini key, and is skipped otherwise - it is the one that
guards the post-filter trap, which is invisible in unit tests and silent in
production.

  python scripts/test_vectordb.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import vectordb  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.routers.contacts import _memories_block, _vector_block, retrieve_context  # noqa: E402

_failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _failures
    if ok:
        print(f"  PASS  {label}")
    else:
        _failures += 1
        print(f"  FAIL  {label}" + (f" - {detail}" if detail else ""))


async def main() -> int:
    settings = get_settings()

    print("\n[1] point IDs are deterministic, so re-indexing overwrites")
    a = vectordb.point_id(vectordb.MEMORY, "6a9d18e886feb4fbe3dd170f")
    b = vectordb.point_id(vectordb.MEMORY, "6a9d18e886feb4fbe3dd170f")
    c = vectordb.point_id(vectordb.MEMORY, "6a9d18e886feb4fbe3dd1710")
    d = vectordb.point_id(vectordb.CONVERSATION, "6a9d18e886feb4fbe3dd170f")
    check("same input gives the same id", a == b)
    check("different memory gives a different id", a != c)
    check("a conversation never collides with a memory", a != d)
    check("ids are UUIDs, which is all the server accepts", len(a) == 36 and a.count("-") == 4)

    print("\n[2] a filtered search must over-fetch, never pass limit straight through")
    # The server post-filters: it takes the top-N by similarity and only then
    # applies the payload filter. Passing `limit` through returns whatever
    # slice of the global top-N happens to belong to this contact - often
    # nothing at all, with no error to notice.
    for limit in (1, 4, 24, 40):
        check(f"limit={limit} fetches a wider window",
              vectordb._fetch_window(limit) > limit,
              f"window={vectordb._fetch_window(limit)}")
    check("the window is capped so a huge collection cannot blow up the call",
          vectordb._fetch_window(100_000) == vectordb._FETCH_CEILING)
    check("the window covers any realistic personal memory store",
          vectordb._fetch_window(1) >= 512)

    print("\n[3] a search with nothing to scope it to refuses rather than leaking")
    check("no contact_id and no phone_key builds no filter",
          vectordb._owner_filter(None, None) is None)
    check("a phone_key alone is enough to scope",
          vectordb._owner_filter(None, "9876543210") is not None)
    check("a contact_id alone is enough to scope",
          vectordb._owner_filter("6a9d18e886feb4fbe3dd170f", None) is not None)
    rows, error = await vectordb.search_memories("anything", limit=5)
    check("an unscoped search returns no rows", rows == [])
    check("...and says why", bool(error))

    print("\n[4] vector rows render in the same shape the Mongo path produces")
    when = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    mongo_block = _memories_block(
        [{"type": "promise", "content": "You promised: send React resources",
          "created_at": when}],
        [{"summary": "Rahul is joining Microsoft.", "recorded_at": when}],
    )
    vector_block = _vector_block([
        {"kind": vectordb.CONVERSATION, "type": "conversation",
         "content": "Rahul is joining Microsoft.",
         "recorded_at": when.isoformat(), "score": 0.61},
        {"kind": vectordb.MEMORY, "type": "promise",
         "content": "You promised: send React resources",
         "created_at": when.isoformat(), "score": 0.66},
    ])
    check("identical rendering, so the prompts read the same either way",
          mongo_block == vector_block,
          f"\n    mongo : {mongo_block!r}\n    vector: {vector_block!r}")
    check("an empty result renders as an empty block", _vector_block([]) == "")

    print("\n[5] retrieval degrades instead of raising when the DB is down")
    saved = vectordb._client, vectordb._available, vectordb._last_error
    vectordb._client, vectordb._available = None, False
    vectordb._last_error = "simulated outage"
    rows, block, source, error = await retrieve_context(
        "how is his mother?", contact_id="6a9d18e886feb4fbe3dd170f", limit=5)
    check("source is None, which is the caller's signal to use Mongo", source is None)
    check("the reason is reported, not swallowed", error == "simulated outage")
    check("no rows and no block, rather than a half-answer", rows == [] and block == "")
    check("index_memories degrades too",
          (await vectordb.index_memories(
              memories=[{"_id": "x", "content": "y", "type": "other"}],
              contact_id="c", contact_name="n", phone_key=None,
              phone_number=None, recorded_at=None))["indexed"] == 0)
    vectordb._client, vectordb._available, vectordb._last_error = saved

    print("\n[6] live server: a scoped search returns the contact's whole slice")
    await vectordb.connect()
    if not vectordb.is_available():
        print("  SKIP  VectorAI DB not reachable at "
              f"{settings.vector_url} - start the container to run this")
    elif not get_settings().gemini_api_key:
        print("  SKIP  no GEMINI_API_KEY, cannot embed a query")
    else:
        from app import db
        await db.connect()
        database = db.get_db()
        target = None
        async for doc in database.contacts.find():
            contact_id = str(doc["_id"])
            indexed = await vectordb.count_for(
                contact_id=contact_id, phone_key=doc.get("phone_key"))
            if indexed and indexed >= 3:
                target = (doc.get("name"), contact_id, doc.get("phone_key"), indexed)
                break
        if target is None:
            print("  SKIP  no contact has enough indexed points; "
                  "run scripts/reindex_vectordb.py")
        else:
            name, contact_id, phone_key, indexed = target
            # The regression that matters: at a small limit the naive
            # implementation returned 0 rows for this exact query.
            rows, error = await vectordb.search_memories(
                "what is going on in their life?",
                contact_id=contact_id, phone_key=phone_key, limit=3)
            check(f"a limit=3 search on {name} returns rows at all",
                  len(rows) > 0, f"error={error}")
            check("...and returns exactly the limit asked for", len(rows) == 3,
                  f"got {len(rows)}")
            rows, error = await vectordb.search_memories(
                "what is going on in their life?",
                contact_id=contact_id, phone_key=phone_key, limit=indexed + 10)
            check(f"a wide search reaches all {indexed} indexed points",
                  len(rows) == indexed, f"got {len(rows)}")
            check("every row belongs to the contact it was scoped to",
                  all(r.get("contact_id") == contact_id
                      or r.get("phone_key") == phone_key for r in rows))
            check("rows come back ordered by relevance",
                  all(rows[i]["score"] >= rows[i + 1]["score"]
                      for i in range(len(rows) - 1)))
        await db.close()
    await vectordb.close()

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED.")
        return 1
    print("All vector DB checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
