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

from core.gemini_resilience import generate_content_with_fallback


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_JSON_CODE_BLOCK = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_EMAIL_TEMPLATE = """Hello {recipient_name},

I hope all is well.

My name is Barry Navan and I am an aspiring entrepreneur building a short-form content agency that turns {channel_content_summary} into high-retention vertical clips formatted specifically for short-form algorithms. Since short-form video is redefining modern marketing, actively circulating these clips across your channels is crucial for reaching younger fans and maximizing visibility.

I loved the "{video_title}" and thought there were a couple of standout sequence moments that work especially well for TikTok and Reels, particularly when {specific_moment_reference}.

To show you what I mean, I edited 3 vertical (9:16) shorts directly from that video, complete with kinetic captions and active framing:

Access the 3 edited clips here:
{drive_link}

No strings attached—feel free to download and post them across YouTube Shorts, Instagram Reels, or TikTok if you like how they look.

If you're interested in a hands-off setup that automatically turns every future video into 3–5 optimized vertical clips, let me know and we can set something up.

Best,
Barry Navan
201-466-4807"""

_SYSTEM_PROMPT = f"""You are an expert short-form video editor and creator outreach writer.
Analyze the supplied transcript and return one JSON object with:
- "clips": exactly three items, each with numeric start_time and end_time (seconds),
  hook_title (string), and reason (string). Target a cohesive, high-retention
  30–50 second self-contained story or tactical breakdown.
- STRICT HOOK START: start_time MUST begin exactly at the start of a complete
  sentence that immediately poses a question, introduces a bold statement, or
  starts an actionable tip. Do not start mid-sentence or mid-breath. Use an exact
  start timestamp from the supplied transcript sentence segments; never estimate
  or invent a timestamp.
- STRICT PUNCHLINE/CONCLUSION END: end_time MUST align with the conclusion of a
  thought or natural sentence period. Under no circumstances cut off mid-thought
  or mid-sentence. Use an exact end timestamp from the supplied transcript
  sentence segments.
  Every clip must be strictly longer than 20 and shorter than 60 seconds. Each
  clip must capture a complete, self-contained thought with a strong hook,
  explanation, and natural conclusion. Use only transcript timestamps and do not invent dialogue.
- "channel_content_summary": one concise factual description of the channel/video
  content, suitable for the email.
- "specific_moment_reference": a concise reference to a specific moment in the
  transcript that fits the email sentence.
- The three clips must not overlap and should come from different parts of the
  video.

The two text fields are inserted into this email template by the application
(do NOT return the email itself):

{_EMAIL_TEMPLATE}

Do not include markdown or extra commentary."""
MIN_GAP_BETWEEN_CLIPS_SECONDS = 1.0


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
) -> dict[str, Any]:
    """Select three transcript clips and draft personalized outreach."""
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
    if not isinstance(clips, list) or len(clips) != 3:
        raise ValueError("The AI response must contain exactly three clips.")

    transcript_start = min(segment["start"] for segment in normalized_segments)
    transcript_end = max(segment["end"] for segment in normalized_segments)
    sentence_starts = [segment["start"] for segment in normalized_segments]
    sentence_ends = [segment["end"] for segment in normalized_segments]
    validated_clips: list[dict[str, Any]] = []
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ValueError(f"Clip {index + 1} must be a JSON object.")
        try:
            start_time = float(clip["start_time"])
            end_time = float(clip["end_time"])
            hook_title = str(clip["hook_title"]).strip()
            reason = str(clip["reason"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Clip {index + 1} has invalid fields.") from exc
        if (
            not math.isfinite(start_time)
            or not math.isfinite(end_time)
            or start_time < transcript_start
            or end_time > transcript_end
        ):
            raise ValueError(f"Clip {index + 1} has invalid timestamps.")
        start_time = min(sentence_starts, key=lambda value: abs(value - start_time))
        end_time = min(sentence_ends, key=lambda value: abs(value - end_time))
        duration = end_time - start_time
        if (
            start_time < transcript_start
            or end_time > transcript_end
            or not 20 < duration < 60
            or not hook_title
            or not reason
        ):
            raise ValueError(
                f"Clip {index + 1} must have valid transcript timestamps, "
                "a duration strictly between 20 and 60 seconds, and non-empty "
                "title and reason."
            )
        validated_clips.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "hook_title": hook_title,
                "reason": reason,
            }
        )

    ordered_clips = sorted(validated_clips, key=lambda item: item["start_time"])
    for previous, current in zip(ordered_clips, ordered_clips[1:]):
        if current["start_time"] < previous["end_time"] + MIN_GAP_BETWEEN_CLIPS_SECONDS:
            raise ValueError(
                "The AI returned overlapping clips "
                f"({previous['start_time']:.1f}-{previous['end_time']:.1f}s and "
                f"{current['start_time']:.1f}-{current['end_time']:.1f}s)."
            )

    channel_content_summary = str(result.get("channel_content_summary", "")).strip()
    specific_moment_reference = str(
        result.get("specific_moment_reference", "")
    ).strip()
    if not channel_content_summary or not specific_moment_reference:
        raise ValueError(
            "The AI response must include channel_content_summary and "
            "specific_moment_reference."
        )

    outreach_email = _EMAIL_TEMPLATE.format(
        recipient_name=channel_name.strip() or "there",
        channel_content_summary=channel_content_summary,
        video_title=video_title.strip(),
        specific_moment_reference=specific_moment_reference,
        drive_link=drive_link.strip(),
    )
    return {"clips": validated_clips, "outreach_email": outreach_email}


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
