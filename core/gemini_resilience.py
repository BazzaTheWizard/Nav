from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)
FALLBACK_MODEL = "gemini-3.5-flash-lite"
RETRY_DELAYS_SECONDS = (3, 6)
RATE_LIMIT_DELAYS_SECONDS = (20, 60)
# Still rate-limited after the waits and the lighter model: the limit is used up
# (usually the daily one). The worker pauses at least this long, longer if Gemini says.
QUOTA_PAUSE_SECONDS = 30 * 60
_RETRY_DELAY = re.compile(r"retryDelay\W+(\d+(?:\.\d+)?)s")


def _retry_after_seconds(exc: Exception) -> float:
    """Gemini's own "retry in N s" hint, if the error carries one, else the default."""
    details = getattr(exc, "details", None)
    try:
        text = json.dumps(details) if details else str(exc)
    except (TypeError, ValueError):
        text = str(exc)
    match = _RETRY_DELAY.search(text)
    return max(QUOTA_PAUSE_SECONDS, float(match.group(1))) if match else QUOTA_PAUSE_SECONDS


def _is_unavailable_error(exc: Exception, server_error_type: type[Exception]) -> bool:
    if not isinstance(exc, server_error_type):
        return False
    status = str(getattr(exc, "status", "") or "").upper()
    code = getattr(exc, "code", None)
    return code == 503 or status == "UNAVAILABLE"


def generate_content_with_fallback(
    generate: Callable[..., Any],
    *,
    model: str,
    fallback_model: str = FALLBACK_MODEL,
    retry_delays: tuple[int, ...] = RETRY_DELAYS_SECONDS,
    **request_kwargs: Any,
) -> Any:
    """Call Gemini with overload fallback and bounded exponential retry."""
    try:
        from google.genai.errors import ClientError, ServerError
    except ImportError as exc:
        raise RuntimeError(
            "Gemini generation requires the google-genai package."
        ) from exc

    active_model = model
    fallback_selected = False
    retry_index = 0
    rate_limit_index = 0
    while True:
        try:
            return generate(model=active_model, **request_kwargs)
        except ClientError as exc:
            # 429 RESOURCE_EXHAUSTED: wait it out, then try the lighter model once.
            if getattr(exc, "code", None) != 429:
                raise
            if rate_limit_index < len(RATE_LIMIT_DELAYS_SECONDS):
                delay = RATE_LIMIT_DELAYS_SECONDS[rate_limit_index]
                rate_limit_index += 1
                logger.warning("Gemini rate limit on %s; waiting %d seconds.", active_model, delay)
                time.sleep(delay)
                continue
            if not fallback_selected and active_model != fallback_model:
                logger.warning("Gemini still rate-limited; switching to %s.", fallback_model)
                active_model = fallback_model
                fallback_selected = True
                rate_limit_index = 0
                continue
            from core.service_pause import ServicePause

            raise ServicePause(
                "Gemini's request limit is used up", _retry_after_seconds(exc), service="gemini",
                hint="It resets on its own (the daily limit at midnight Pacific).",
            ) from exc
        except ServerError as exc:
            if (
                _is_unavailable_error(exc, ServerError)
                and active_model != fallback_model
                and not fallback_selected
            ):
                if retry_index < len(retry_delays):
                    delay = retry_delays[retry_index]
                    retry_index += 1
                    logger.warning(
                        "Gemini model %s returned 503 UNAVAILABLE; retrying in %d "
                        "seconds before switching models (retry %d/%d).",
                        active_model,
                        delay,
                        retry_index,
                        len(retry_delays),
                    )
                    time.sleep(delay)
                    continue
                logger.warning(
                    "Gemini model %s returned 503 UNAVAILABLE; falling back to %s.",
                    active_model,
                    fallback_model,
                )
                active_model = fallback_model
                fallback_selected = True
                retry_index = 0
                continue
            if retry_index >= len(retry_delays):
                raise
            delay = retry_delays[retry_index]
            retry_index += 1
            logger.warning(
                "Gemini model %s returned a server error (%s); retrying in %d seconds "
                "(retry %d/%d).",
                active_model,
                exc,
                delay,
                retry_index,
                len(retry_delays),
            )
            time.sleep(delay)


def gemini_thinking_config(types: Any) -> Any:
    """Thinking effort for Gemini calls (GEMINI_THINKING_LEVEL, default "low").

    Picking clips doesn't need deep reasoning, and Gemini 3.8 Flash bills its
    thinking tokens as output, so "low" is cheaper and faster. Set the variable
    to "medium" or "high" for more effort, or "" to use the model default.
    """
    level = os.getenv("GEMINI_THINKING_LEVEL", "low").strip().upper()
    if not level:
        return None
    try:
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel(level))
    except (AttributeError, ValueError):
        return None
