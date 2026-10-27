from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)
FALLBACK_MODEL = "gemini-3.5-flash-lite"
RETRY_DELAYS_SECONDS = (3, 6)


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
        from google.genai.errors import ServerError
    except ImportError as exc:
        raise RuntimeError(
            "Gemini generation requires the google-genai package."
        ) from exc

    active_model = model
    fallback_selected = False
    retry_index = 0
    while True:
        try:
            return generate(model=active_model, **request_kwargs)
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
