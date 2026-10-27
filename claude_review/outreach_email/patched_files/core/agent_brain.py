from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.gemini_resilience import (
    gemini_thinking_config,
    generate_content_with_fallback,
)
from core.outreach import build_outreach_email, clean_topic


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_JSON_CODE_BLOCK = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
DEFAULT_CANDIDATE_COUNT = 5

_SYSTEM_PROMPT = """You are an expert short-form video editor.
Analyze the supplied transcript and return one JSON object with:
- "clips": exactly "candidate_count" items (given in the request), ranked best
  first, each with numeric start_time and end_time (seconds), hook_title (string,
  under 60 characters, shown on screen as the clip's hook), and reason (string).
  Target a cohesive, high-retention 30–50 second self-contained story or
  tactical breakdown.
- STRICT HOOK START: start_time MUST begin exactly at the start of a complete
  sentence that immediately poses a question, introduces a bold statement, or
  starts an actionable tip. Do not start mid-sentence or mid-breath. Use an exact
  start timestamp from the supplied transcript sentence segments; never estimate
  or invent a timestamp.
- STRICT PUNCHLINE/CONCLUSION END: end_time MUST align with the conclusion of a
  thought or natural sentence period. Under no circumstances cut off mid-thought
  or mid-sentence. Use an exact end timestamp from the supplied transcript
  sentence segments.
  Every clip must be between 30 and 60 seconds long. Each
  clip must capture a complete, self-contained thought with a strong hook,
  explanation, and natural conclusion. Use only transcript timestamps and do not invent dialogue.
- "video_topic": the specific topic of this video as a short phrase (3–10 words,
  lowercase except names), written to complete the sentence "I cut 3 sample
  Shorts from your recent video on ___". Be specific to what the video actually
  covers, e.g. "squatting pain-free with knee arthritis", not "fitness".
- The clips must not overlap and should come from different parts of the
  video.
- When "most_replayed" moments are supplied, YouTube viewers rewatched those
  parts the most. Prefer clips that contain them, as long as each clip still
  stands on its own.

The application writes the outreach email itself; do NOT return an email.
Do not include markdown or extra commentary."""


def _format_transcript_segments(
    segments: list[dict[str, float | str]],
) -> str:
    return "\n".join(
        f'{segment["start"]:.2f}-{segment["end"]:.2f}: {segment["text"]}'
        for segment in segments
    )


def _parse_json_response(response_text: str) -> dict[str, Any]:
    cleaned = response_text.strip()
    code_block = _JSON_CODE_BLOCK.fullmatch(cleaned)
    if code_block is not None:
        cleaned = code_block.group(1).strip()
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("The AI response must be a JSON object.")
    return payload


def _request_openai(api_key: str, prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty response.")
    return content


def _request_gemini(api_key: str, prompt: str) -> str:
    try:
        genai = import_module("google.genai")
        types = import_module("google.genai.types")
    except ImportError as exc:
        raise RuntimeError(
            "GEMINI_API_KEY is set, but google-genai is not installed. "
            "Install it with `python -m pip install google-genai`."
        ) from exc

    client = genai.Client(api_key=api_key)
    response = generate_content_with_fallback(
        client.models.generate_content,
        model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            thinking_config=gemini_thinking_config(types),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    return response.text


def select_clips_and_draft_pitch(
    transcript_segments: list[dict[str, Any]],
    channel_name: str,
    video_title: str,
    drive_link: str = "",
    *,
    candidate_count: int = DEFAULT_CANDIDATE_COUNT,
    replay_peaks: list[tuple[float, float, float]] | None = None,
) -> dict[str, Any]:
    """Select transcript clips and build the outreach email (see core/outreach.py)."""
    if not transcript_segments:
        raise ValueError("transcript_segments must not be empty.")
    normalized_segments: list[dict[str, float | str]] = []
    for index, segment in enumerate(transcript_segments):
        try:
            start = float(segment["start"])
            end = float(segment["end"])
            text = str(segment["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Transcript segment {index} must contain start, end, and text."
            ) from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"Transcript segment {index} has invalid timestamps.")
        if not text:
            continue
        normalized_segments.append({"start": start, "end": end, "text": text})
    if not normalized_segments:
        raise ValueError("transcript_segments must contain text.")

    load_dotenv(_PROJECT_ROOT / ".env")
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    request_payload = json.dumps(
        {
            "transcript": _format_transcript_segments(normalized_segments),
            "channel_name": channel_name,
            "video_title": video_title,
            "drive_link": drive_link,
            "candidate_count": candidate_count,
            "most_replayed": [
                {"start": round(start, 1), "end": round(end, 1), "intensity": round(value, 2)}
                for start, end, value in (replay_peaks or [])
            ],
        },
        ensure_ascii=False,
    )
    if gemini_key:
        response_text = _request_gemini(gemini_key, request_payload)
    elif openai_key:
        response_text = _request_openai(openai_key, request_payload)
    else:
        raise RuntimeError(
            "Configure GEMINI_API_KEY or OPENAI_API_KEY in the project .env file."
        )

    result = _parse_json_response(response_text)
    clips = result.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("The AI response must contain a list of clips.")

    # Keep every well-formed candidate in rank order. Snapping to sentences,
    # the 30-60 s rule and overlap checks happen in
    # hook_selector.finalize_candidates, so one bad pick no longer discards all.
    transcript_start = min(segment["start"] for segment in normalized_segments)
    transcript_end = max(segment["end"] for segment in normalized_segments)
    validated_clips: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        try:
            start_time = float(clip["start_time"])
            end_time = float(clip["end_time"])
            hook_title = str(clip["hook_title"]).strip()
            reason = str(clip["reason"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(start_time)
            or not math.isfinite(end_time)
            or end_time <= start_time
            or start_time < transcript_start - 1
            or start_time >= transcript_end
            or not hook_title
        ):
            continue
        validated_clips.append(
            {
                "start_time": start_time,
                "end_time": min(end_time, transcript_end),
                "hook_title": hook_title,
                "reason": reason or hook_title,
            }
        )
    if not validated_clips:
        raise ValueError("The AI response did not contain any usable clips.")

    # A missing topic is not worth failing the lead over: the email falls back to
    # the video title.
    video_topic = clean_topic(result.get("video_topic"))
    outreach_email = build_outreach_email(
        channel_name,
        video_title=video_title,
        video_topic=video_topic,
        drive_link=drive_link,
    )
    return {"clips": validated_clips, "outreach_email": outreach_email, "video_topic": video_topic}


def qualify_channel_candidate(
    subscriber_count: int | None,
    recent_durations: Sequence[float],
    recent_is_short_flags: Sequence[bool],
) -> bool:
    """Return whether a channel meets the size, duration, and recent Shorts criteria."""
    if subscriber_count is not None and subscriber_count >= 125_000:
        return False
    if subscriber_count is not None and subscriber_count < 0:
        return False
    if not recent_durations or not recent_is_short_flags:
        return False
    durations = [float(duration) for duration in recent_durations]
    if any(not math.isfinite(duration) or duration < 0 for duration in durations):
        return False
    average_duration = sum(durations) / len(durations)
    if not 240 <= average_duration <= 3000:
        return False
    return not any(recent_is_short_flags[-3:])
