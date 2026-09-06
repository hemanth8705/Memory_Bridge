"""Confirm the Gemini API key works and check which flash-tier models it can
actually reach. Reads GEMINI_API_KEY from .env - no key is ever printed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai  # noqa: E402
from google.genai.errors import APIError  # noqa: E402

from app.config import get_settings  # noqa: E402

CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-2.0-flash",
]


def main() -> None:
    key = get_settings().gemini_api_key
    if not key:
        print("No GEMINI_API_KEY in .env")
        return

    client = genai.Client(api_key=key)
    print("Listing models this key can access...")
    try:
        names = {m.name.replace("models/", "") for m in client.models.list()}
    except APIError as exc:
        print("API ERROR listing models:", exc)
        return
    except Exception as exc:
        print("ERROR listing models:", type(exc).__name__, exc)
        return

    print(f"  {len(names)} models total\n")
    print("Flash-tier candidates:")
    for name in CANDIDATES:
        mark = "AVAILABLE" if name in names else "not listed"
        print(f"  {name:28s} -> {mark}")

    # Listing can lag behind what generate_content actually accepts, so make a
    # real (tiny, cheap) call against each candidate to be sure.
    print("\nActually calling generate_content on each candidate:")
    for name in CANDIDATES:
        try:
            response = client.models.generate_content(
                model=name, contents="Reply with exactly one word: OK"
            )
            text = (response.text or "").strip()
            print(f"  {name:28s} -> OK, replied: {text!r}")
        except Exception as exc:
            print(f"  {name:28s} -> FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
