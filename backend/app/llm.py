"""Gemini: relationship-memory extraction and question answering.

The API key is supplied by the user from the Flutter app and passed in per call.
A server-side GEMINI_API_KEY is only a fallback for local smoke tests.
"""
import json
from typing import Any, Optional

from .config import get_settings

SYSTEM_PROMPT = """You are the memory of an AI relationship assistant built for people with ADHD.

You read transcripts of phone calls the user had with people they care about, and
you extract the details a person with ADHD is most likely to lose track of - the
details that, remembered weeks later, make a relationship feel cared for.

Prioritise:
  - important personal details about the other person
  - promises and commitments (in BOTH directions - what the user promised, and
    what the other person promised)
  - future plans, trips, appointments
  - birthdays and other important dates
  - family members and what is happening with them
  - jobs, job changes, companies, studies
  - travel and locations
  - interests, hobbies, things they are into
  - emotional context (stressed, excited, grieving, proud)
  - natural follow-up opportunities for the next conversation

Hard rules:
  - NEVER invent information. Extract only what is actually supported by the
    transcript.
  - If a detail is implied but not certain, still record it but set
    "confidence" to "low" and keep the wording hedged.
  - Prefer specific over general. "Joining Microsoft next month" beats "has news
    about work".
  - Write each value as a short standalone sentence that will still make sense
    read on its own in three weeks, without the transcript next to it.
  - Keep relative dates exactly as spoken ("next Monday"), and also copy them
    into the date field so they can be resolved later.
  - If the transcript contains nothing worth remembering, return empty arrays.
    An empty result is correct and useful; padding it is not.
"""

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three sentences summarising the conversation.",
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "job, travel, health, family, study, event, personal, other",
                    },
                    "value": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["type", "value", "confidence"],
            },
        },
        "people": {
            "type": "array",
            "description": "Other people mentioned in the conversation.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "relationship": {"type": "string"},
                    "event": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["relationship"],
            },
        },
        "promises": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "owed_by": {
                        "type": "string",
                        "enum": ["user", "contact", "unclear"],
                        "description": "Who owes it. user means the app owner promised it.",
                    },
                    "due": {"type": "string"},
                },
                "required": ["description", "owed_by"],
            },
        },
        "follow_ups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["description"],
            },
        },
        "important_dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "when": {"type": "string"},
                },
                "required": ["what", "when"],
            },
        },
        "interests": {"type": "array", "items": {"type": "string"}},
        "locations": {"type": "array", "items": {"type": "string"}},
        "companies": {"type": "array", "items": {"type": "string"}},
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Three to six short topic labels for this conversation.",
        },
        "emotional_context": {"type": "string"},
    },
    "required": ["summary", "facts", "promises", "follow_ups", "topics"],
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "suggested_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer"],
}


class LLMError(RuntimeError):
    pass


def resolve_api_key(supplied: Optional[str]) -> str:
    key = (supplied or "").strip() or get_settings().gemini_api_key
    if not key:
        raise LLMError(
            "No Gemini API key. Set it on the Setup screen in the app, or "
            "GEMINI_API_KEY in .env for local testing."
        )
    return key


def _generate(api_key: str, prompt: str, schema: dict, system: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=get_settings().gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise LLMError("Gemini returned an empty response.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Gemini returned non-JSON output: {text[:400]}") from exc


def extract_memories(transcript: str, contact_name: str, api_key: Optional[str]) -> dict:
    """Transcript -> structured relationship memories. Blocking; run in a thread."""
    key = resolve_api_key(api_key)
    who = contact_name or "an unknown caller"
    prompt = (
        f"This is the transcript of a phone call between the user and {who}.\n"
        f"Extract what is worth remembering about {who} and their life.\n\n"
        f"--- TRANSCRIPT ---\n{transcript}\n--- END TRANSCRIPT ---"
    )
    return _generate(key, prompt, EXTRACTION_SCHEMA, SYSTEM_PROMPT)


ANSWER_SYSTEM = """You are an AI relationship memory assistant for a person with ADHD.

You are given remembered details about one person in the user's life, gathered
from past phone calls, plus a question from the user.

Answer from the supplied memories only. If the memories do not contain the
answer, say so plainly - do not guess or fill in plausible details. Be warm,
concrete and brief: this is being read on a phone, often right before a call.
Mention when something was recorded if the timing matters (for example an
upcoming event that may already have happened).

Also propose two to four natural questions the user could ask this person next
time, grounded in the memories.
"""


def answer_question(question: str, contact_name: str, memories_block: str,
                    api_key: Optional[str]) -> dict:
    key = resolve_api_key(api_key)
    prompt = (
        f"Person: {contact_name}\n\n"
        f"--- REMEMBERED ABOUT THEM ---\n{memories_block or '(nothing recorded yet)'}\n"
        f"--- END ---\n\n"
        f"User's question: {question}"
    )
    return _generate(key, prompt, ANSWER_SCHEMA, ANSWER_SYSTEM)
