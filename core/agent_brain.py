from __future__ import annotations

import json
import logging
import math
import os
import re
from importlib import import_module
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.service_pause import ServicePause
from core.gemini_resilience import (
    gemini_thinking_config,
    generate_content_with_fallback,
)
from core.outreach import (
    build_outreach_email,
    build_subject,
    clean_first_name,
    clean_format,
    clean_key_point,
    clean_short_topic,
    clean_topic,
    clean_usual_take,
)


logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_JSON_CODE_BLOCK = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
DEFAULT_CANDIDATE_COUNT = 5

_SYSTEM_PROMPT = """You are an expert short-form video editor.
Analyze the supplied transcript and return one JSON object with:
- "clips": exactly "candidate_count" items (given in the request), ranked best
  first, each with numeric start_time and end_time (seconds), hook_title (string,
  under 60 characters, shown on screen as the clip's hook), reason (string),
  point and usual_take (both described below).
- Each clip's "point": what the speaker argues, reveals or shows in that clip and
  why it lands, as one phrase (4–14 words, lowercase except names, no quotes)
  that completes "Your point at 12:34 about ___ stuck with me". Use what they
  actually say in that clip, e.g. "parents doing more teaching than any
  classroom", not "parenting".
- Each clip's "usual_take": only if that clip goes against a common belief, the
  subject of that belief in 1–4 words (lowercase except names) that completes
  "especially how it flips the usual take on ___", e.g. "homework" or
  "stretching before lifting". Use "" when the clip doesn't contradict a common
  view; never invent a contrast.
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
  lowercase except names), written to complete the sentence "Your recent video
  on ___ stuck with me". Be specific to what the video actually
  covers, e.g. "squatting pain-free with knee arthritis", not "fitness".
- "key_point": one specific insight, claim or story from this video as a short
  phrase (5–15 words, lowercase except names, no quotes), written to complete
  "Your point about ___ stuck with me". Use what the
  speaker actually says, e.g. "squatting below parallel being fine for healthy
  knees", not a generic summary.
- "short_topic": what this video is about in 1–3 words (lowercase except
  names) that completes the email subject "3 Shorts from your ___ episode".
  Aim for 9–23 characters, e.g. "knee arthritis", "Michigan offense",
  "early parenting". Never the channel name or the word "video"/"episode".
- "format": "episode" for a podcast, interview or conversation between two or
  more people; "video" for a solo or educational upload.
- "host_first_name": the first name of the channel's host, only if the
  transcript makes it clear (e.g. "I'm Marcela and this is..."). Use "" when
  unsure or when only guests are named.
- The clips must not overlap and should come from different parts of the
  video.
- When "most_replayed" moments are supplied, YouTube viewers rewatched those
  parts the most. Prefer clips that contain them, as long as each clip still
  stands on its own.
- Prefer moments with something concrete: a story, a number, a strong opinion,
  a surprising claim, or a clear how-to.
- Never pick sponsor reads, intros, outros, or "like and subscribe" asks.
- Never start a clip on a word that continues or answers something said before
  ("And", "But", "Because", "Yeah", "Exactly", "That's why", "Like I said").

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


def _request_openai(api_key: str, prompt: str, system_prompt: str = _SYSTEM_PROMPT) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty response.")
    return content


def _request_gemini(api_key: str, prompt: str, system_prompt: str = _SYSTEM_PROMPT) -> str:
    try:
        genai = import_module("google.genai")
        types = import_module("google.genai.types")
    except ImportError as exc:
        raise RuntimeError(
            "GEMINI_API_KEY is set, but google-genai is not installed. "
            "Install it with `python -m pip install google-genai`."
        ) from exc

    # No timeout is the SDK default: one stuck request would stall the worker forever.
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
    response = generate_content_with_fallback(
        client.models.generate_content,
        model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            thinking_config=gemini_thinking_config(types),
            # No tools are passed, so automatic function calling only adds a log line.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    return response.text


AI_ATTEMPTS = 2
GEMINI_TIMEOUT_MS = 120_000  # per request; picking clips from a 2-hour transcript takes ~15 s


def _validated_clips(result: dict[str, Any], transcript_start: float, transcript_end: float) -> list[dict[str, Any]]:
    """Every well-formed candidate, in rank order. Snapping to sentences, the 30-60 s
    rule and overlap checks happen in hook_selector.finalize_candidates."""
    clips = result.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("The AI response must contain a list of clips.")
    validated: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        try:
            start_time = float(clip["start_time"])
            end_time = float(clip["end_time"])
            hook_title = str(clip["hook_title"]).strip()
            reason = str(clip.get("reason") or "").strip()
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
        validated.append(
            {
                "start_time": start_time,
                "end_time": min(end_time, transcript_end),
                "hook_title": hook_title,
                "reason": reason or hook_title,
                # For the email's "Your point at 12:34 about ___" line.
                "point": clean_key_point(clip.get("point")),
                "usual_take": clean_usual_take(clip.get("usual_take")),
            }
        )
    if not validated:
        raise ValueError("The AI response did not contain any usable clips.")
    return validated


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
    if not gemini_key and not openai_key:
        raise RuntimeError(
            "Configure GEMINI_API_KEY or OPENAI_API_KEY in the project .env file."
        )
    transcript_start = min(segment["start"] for segment in normalized_segments)
    transcript_end = max(segment["end"] for segment in normalized_segments)

    # One retry on a malformed answer: much cheaper than failing the lead, which
    # makes the worker download and transcribe the whole video again.
    last_error: Exception | None = None
    for attempt in range(AI_ATTEMPTS):
        if gemini_key:
            response_text = _request_gemini(gemini_key, request_payload)
        else:
            response_text = _request_openai(str(openai_key), request_payload)
        try:
            result = _parse_json_response(response_text)
            validated_clips = _validated_clips(result, transcript_start, transcript_end)
            break
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning("AI answer unusable (attempt %d/%d): %s", attempt + 1, AI_ATTEMPTS, exc)
    else:
        assert last_error is not None
        raise last_error

    # A missing topic is not worth failing the lead over: the email falls back to
    # the video title.
    video_topic = clean_topic(result.get("video_topic"))
    key_point = clean_key_point(result.get("key_point"))
    host_first_name = clean_first_name(result.get("host_first_name"))
    short_topic = clean_short_topic(result.get("short_topic"))
    content_format = clean_format(result.get("format"))
    # A first draft before the clips are final; core/pipeline.py rebuilds it around
    # the best clip's start time and point.
    outreach_email = build_outreach_email(
        channel_name,
        video_title=video_title,
        video_topic=video_topic,
        drive_link=drive_link,
        key_point=key_point,
        greeting_name=host_first_name,
        content_format=content_format,
    )
    return {
        "clips": validated_clips,
        "outreach_email": outreach_email,
        "subject": build_subject(short_topic, content_format),
        "video_topic": video_topic,
        "key_point": key_point,
        "host_first_name": host_first_name,
        "short_topic": short_topic,
        "format": content_format,
    }


# --- Second opinion on the actual clips --------------------------------------------
# The first pass picks moments from the whole transcript. After the clips are
# snapped to sentences, this pass reads each clip's exact words and scores it the
# way a viewer would meet it: cold, with no context.
_JUDGE_PROMPT = """You review candidate Shorts cut from one long video.
For each numbered clip (its exact words), give three scores from 1 to 10:
- hook: do the first one or two sentences give someone scrolling a reason to keep watching?
- standalone: does it make sense to someone who has not seen the rest of the video?
  Score low if it relies on something said earlier.
- payoff: does it end on a point, answer, story ending or punchline instead of trailing off?
Return JSON only: {"scores": [{"clip": 1, "hook": 7, "standalone": 8, "payoff": 6}, ...]}
with exactly one entry per clip."""
# Per clip. Round 17: was 160, which cut the ending (the "payoff" score) off any clip
# longer than ~54 s at the 177 words a minute these creators speak. 300 covers 60 s
# at 300 words a minute.
JUDGE_MAX_WORDS = 300


def _judge_scores(result: dict[str, Any], count: int) -> list[int]:
    """0-100 per clip from the judge's JSON; hook and standalone count double."""
    entries = result.get("scores")
    if not isinstance(entries, list):
        raise ValueError("The judge answer has no scores list.")
    by_clip: dict[int, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry["clip"])
            parts = [min(10.0, max(1.0, float(entry[key]))) for key in ("hook", "standalone", "payoff")]
        except (KeyError, TypeError, ValueError):
            continue
        hook, standalone, payoff = parts
        by_clip[number] = round((2 * hook + 2 * standalone + payoff) / 50 * 100)
    if sorted(by_clip) != list(range(1, count + 1)):
        raise ValueError("The judge did not score every clip exactly once.")
    return [by_clip[number] for number in range(1, count + 1)]


def judge_clips(clip_texts: list[str]) -> list[int] | None:
    """Score each clip's words 0-100. None when there's no AI key or no usable answer
    after AI_ATTEMPTS tries (the worker's gate then waits and tries the creator again;
    the app keeps the first pass's ranking). A used-up Gemini limit (ServicePause) is
    passed on, so the worker pauses instead of retrying straight away."""
    if not clip_texts:
        return None
    load_dotenv(_PROJECT_ROOT / ".env")
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    if not gemini_key and not openai_key:
        return None
    prompt = "\n\n".join(
        f"Clip {number}: " + " ".join(text.split()[:JUDGE_MAX_WORDS])
        for number, text in enumerate(clip_texts, start=1)
    )
    for attempt in range(1, AI_ATTEMPTS + 1):
        try:
            if gemini_key:
                answer = _request_gemini(gemini_key, prompt, _JUDGE_PROMPT)
            else:
                answer = _request_openai(str(openai_key), prompt, _JUDGE_PROMPT)
            return _judge_scores(_parse_json_response(answer), len(clip_texts))
        except ServicePause:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as None, the caller decides
            logger.warning("Clip judge answer unusable (attempt %d/%d): %s", attempt, AI_ATTEMPTS, exc)
    return None
