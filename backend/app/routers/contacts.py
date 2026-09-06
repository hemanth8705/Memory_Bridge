"""Contacts, their memories, and question answering over them."""
import asyncio
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import llm
from ..db import get_db

router = APIRouter()


def _serialize_contact(doc: dict) -> dict:
    return {
        "contact_id": str(doc["_id"]),
        "name": doc.get("name") or "Unknown caller",
        "phone_number": doc.get("phone_number"),
        "conversation_count": doc.get("conversation_count", 0),
        "last_conversation_at": doc.get("last_conversation_at"),
    }


@router.get("/contacts")
async def list_contacts():
    db = get_db()
    out = []
    async for doc in db.contacts.find().sort("last_conversation_at", -1):
        out.append(_serialize_contact(doc))
    return {"contacts": out}


async def _load_contact(contact_id: str) -> dict:
    db = get_db()
    try:
        oid = ObjectId(contact_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed contact_id")
    doc = await db.contacts.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    return doc


@router.get("/contacts/{contact_id}/memories")
async def contact_memories(contact_id: str):
    """Everything remembered about one person, grouped for the contact screen."""
    db = get_db()
    contact = await _load_contact(contact_id)

    memories = []
    async for doc in db.memories.find({"contact_id": contact_id}).sort("created_at", -1):
        doc["memory_id"] = str(doc.pop("_id"))
        memories.append(doc)

    conversations = []
    # Chronological order of the actual calls, not the order we processed
    # them in - those diverge whenever a backlog is scanned all at once.
    async for doc in db.recordings.find(
        {"contact_id": contact_id, "status": "completed"}
    ).sort("recorded_at", -1):
        conversations.append({
            "hash": doc.get("hash"),
            "filename": doc.get("filename"),
            "summary": doc.get("summary", ""),
            "topics": doc.get("topics", []),
            "recorded_at": doc.get("recorded_at"),
            "processed_at": doc.get("processed_at"),
        })

    topics: list[str] = []
    for conversation in conversations:
        for topic in conversation.get("topics") or []:
            if topic not in topics:
                topics.append(topic)

    grouped: dict[str, list[dict]] = {}
    for memory in memories:
        grouped.setdefault(memory.get("type", "other"), []).append(memory)

    return {
        **_serialize_contact(contact),
        "topics": topics,
        "memories": memories,
        "grouped": grouped,
        "conversations": conversations,
    }


class SearchRequest(BaseModel):
    query: str
    contact: Optional[str] = None
    contact_id: Optional[str] = None


def _memories_block(memories: list[dict], conversations: list[dict]) -> str:
    lines = []
    for conversation in conversations:
        when = conversation.get("recorded_at") or conversation.get("processed_at")
        stamp = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else "unknown date"
        if conversation.get("summary"):
            lines.append(f"[conversation on {stamp}] {conversation['summary']}")
    for memory in memories:
        when = memory.get("created_at")
        stamp = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else "unknown date"
        lines.append(f"[{memory.get('type', 'other')}, recorded {stamp}] {memory.get('content', '')}")
    return "\n".join(lines)


@router.post("/memory/search")
async def search_memory(
    payload: SearchRequest,
    x_gemini_api_key: Optional[str] = Header(None, alias="X-Gemini-Api-Key"),
):
    """Ask a question about a person; answered from their stored memories."""
    db = get_db()

    contact = None
    if payload.contact_id:
        contact = await _load_contact(payload.contact_id)
    elif payload.contact:
        contact = await db.contacts.find_one(
            {"name": {"$regex": payload.contact, "$options": "i"}}
        )
    if contact is None:
        raise HTTPException(
            status_code=404,
            detail="No matching contact. Pass contact_id, or a name that exists.",
        )

    contact_id = str(contact["_id"])
    memories = []
    async for doc in db.memories.find({"contact_id": contact_id}).sort("created_at", -1).limit(200):
        doc.pop("_id", None)
        memories.append(doc)
    conversations = []
    async for doc in db.recordings.find(
        {"contact_id": contact_id, "status": "completed"}
    ).sort("recorded_at", -1).limit(50):
        conversations.append(doc)

    block = _memories_block(memories, conversations)
    name = contact.get("name") or "this person"
    try:
        result = await asyncio.to_thread(
            llm.answer_question, payload.query, name, block, x_gemini_api_key
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "contact_id": contact_id,
        "contact_name": name,
        "query": payload.query,
        "answer": result.get("answer", ""),
        "suggested_questions": result.get("suggested_questions", []),
        "memories_considered": len(memories),
    }
