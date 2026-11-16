"""Round 59: what a shot shows, in tags, from three of its frames (Gemini, GEMINI_API_KEY).

Optional: without a key the library still tags shots from folder/file names, brightness and
motion. With it, every shot also gets scene tags from the same vocabulary the script writer
uses (core.narrate.script.SYSTEM_PROMPT), so a line tagged [beach, sunset] finds beach sunsets.
"""
from __future__ import annotations

import json
import os
from importlib import import_module

import numpy as np

VOCAB = ("driving flying boat walking running city beach swamp highway rooftop interior shop garage "
         "day night sunset rain chase police crowd nature aerial menu hud").split()

PROMPT = ("These are three frames from one shot of GTA 6 gameplay. Return a JSON list of the tags that "
          "clearly apply, chosen only from: " + ", ".join(VOCAB) + ". Add \"menu\" if a pause menu, map "
          "or loading screen fills the frame. Return only the JSON list.")


def available() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def gemini_tagger():
    """A tagger for Library.ingest (None when no GEMINI_API_KEY is set)."""
    if not available():
        return None
    import cv2

    genai = import_module("google.genai")
    types = import_module("google.genai.types")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=60_000))
    model = os.getenv("GEMINI_TAG_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))

    def tag(frames: list[np.ndarray]) -> list[str]:
        parts = [types.Part.from_bytes(data=cv2.imencode(".jpg", f)[1].tobytes(), mime_type="image/jpeg")
                 for f in frames]
        response = client.models.generate_content(
            model=model, contents=[*parts, PROMPT],
            config=types.GenerateContentConfig(response_mime_type="application/json"))
        found = json.loads(response.text or "[]")
        return [t for t in (str(x).strip().lower() for x in found) if t in VOCAB]

    return tag
