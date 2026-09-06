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
    """Call Gemini and return parsed JSON.

    Every failure mode - bad key, quota, network, safety block, truncated or
    non-JSON output - surfaces as LLMError so callers have one thing to catch.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise LLMError(f"google-genai is not installed: {exc}") from exc

    model = get_settings().gemini_model
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.2,
            ),
        )
    except Exception as exc:
        raise LLMError(f"{type(exc).__name__} calling {model}: {exc}") from exc

    try:
        text = (response.text or "").strip()
    except Exception as exc:
        # A response blocked by a safety filter has no .text at all.
        raise LLMError(f"{model} returned no usable content: {exc}") from exc

    if not text:
        raise LLMError(f"{model} returned an empty response.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{model} returned non-JSON output: {text[:400]}") from exc

    if not isinstance(parsed, dict):
        raise LLMError(
            f"{model} returned {type(parsed).__name__}, expected a JSON object."
        )
    return parsed


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

Also propose two to four questions the user could ask this person next time.
Every question must be anchored to a specific remembered detail - name the
thing, do not gesture at it. "How was the Goa trip with your college friends?"
is right; "How was your trip?" is not. Never propose a question that would make
sense addressed to a stranger.
"""

# --- incoming-call briefing ------------------------------------------------

CALLER_BRIEFING_SYSTEM = """You prepare a person with ADHD for a phone call that is
ringing RIGHT NOW.

You are given what the user remembers about the caller, gathered from previous
calls. You produce two things they can read in the few seconds before they
answer: questions to ask, and context to recall.

THE STANDARD YOUR QUESTIONS MUST MEET

Every question must sound like it came from someone who genuinely remembers
this person's life - not from someone reading a database. Anchor each question
to a specific remembered detail: name the company, the place, the person, the
event. If a question would still make sense asked of a total stranger, it has
failed and you must replace it.

  BAD - generic, could be anyone:
    "How are you?"
    "What's up?"
    "How was your trip?"
    "How is everything going?"
    "How is work?"

  GOOD - only makes sense for THIS person:
    memory: "going to Goa with college friends next weekend"
      -> "How was the Goa trip with your college friends?"
      -> "Did you get to the places you were planning to see?"
    memory: "joining Microsoft next month"
      -> "How has the transition into Microsoft been?"
      -> "Is the role what you expected when we last spoke?"
    memory: "mother's surgery is on Monday"
      -> "How is your mom recovering after the surgery?"
      -> "Is everything getting back to normal at home?"

Lead with what matters most to them emotionally, not what is most recent. A
parent's surgery outranks a job change; a job change outranks a hobby.

Handle time honestly. These memories are weeks old, so something described as
upcoming has probably already happened - ask about it in the past tense ("How
was the trip?"), never as though it is still ahead of them.

Be warm and curious, never interrogating. Do not stack multiple questions into
one. Do not be formal or transactional. Do not pry into anything the memories
treat as sensitive beyond what the person themselves volunteered. If the user
owes this person something, phrase it as a natural opening rather than an
apology they have to perform.

Return 3 to 5 questions. Fewer excellent ones beat a long list - if the
memories only support two good questions, return two.

THE CONTEXT LINES

Short scannable bullets, read while the phone is ringing. Six words to a dozen,
no sentences, no preamble. Prioritise: things the user promised or owes, events
that have happened since they last spoke, major life changes, then interests
worth mentioning. Write them as facts to recall - "Joined Microsoft last month",
"Mom had surgery", "You promised to send React resources".

Return at most 6 context lines.

ABSOLUTE RULE

Use only what is in the supplied memories. Never invent a detail, a name, a
date, or an event. If the memories are thin, return fewer items. If there is
nothing worth saying, return empty lists - that is a correct answer.
"""

BRIEFING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": "3-5 specific, empathetic questions anchored to real memories.",
            "items": {"type": "string"},
        },
        "context": {
            "type": "array",
            "description": "Up to 6 short scannable recall bullets.",
            "items": {"type": "string"},
        },
    },
    "required": ["questions", "context"],
}


def caller_briefing(contact_name: str, memories_block: str,
                    api_key: Optional[str]) -> dict:
    """Memories -> {questions, context} for the incoming-call popup.

    Blocking; callers run it in a thread.
    """
    key = resolve_api_key(api_key)
    who = contact_name or "this caller"
    prompt = (
        f"{who} is calling the user right now.\n\n"
        f"--- WHAT THE USER REMEMBERS ABOUT {who.upper()} ---\n"
        f"{memories_block or '(nothing recorded yet)'}\n"
        f"--- END ---\n\n"
        f"Prepare them for the call."
    )
    return _generate(key, prompt, BRIEFING_SCHEMA, CALLER_BRIEFING_SYSTEM)


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
