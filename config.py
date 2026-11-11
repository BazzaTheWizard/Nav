from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_model: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: str | None
    raw_dir: Path
    temp_dir: Path
    output_dir: Path
    # Seconds the clip's hook title stays on screen (0 turns it off).
    hook_title_seconds: float = 4.0
    # Hook title card look: "light" (white card, dark text) or "dark".
    hook_title_style: str = "light"
    # Colour the word being spoken inside each caption card.
    caption_highlight: bool = True
    # Round 28: the graphics director adds each clip's graphics automatically.
    auto_graphics: bool = True
    # Round 40: clips of 28 s+ open on a 2.5-4 s climax teaser from their back half when a
    # strong hinge line is found (core.climax_hook). ENABLE_CLIMAX_HOOK=0 turns it off.
    enable_climax_hook: bool = True
    # Round 45 (core.executive): "executive_minimal" (default: no graphics, one lower-third
    # ID), "none", or "full" (the creator graphics of rounds 28-41). VISUAL_OVERLAYS in .env.
    visual_overlays: str = "executive_minimal"
    # Round 45 (core.linkedin_post): a ready-to-post LinkedIn caption next to every clip.
    linkedin_posts: bool = True

    @property
    def b2b_clean_mode(self) -> bool:
        return self.visual_overlays != "full"


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    language = os.getenv("WHISPER_LANGUAGE", "").strip()

    settings = Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        whisper_device=os.getenv("WHISPER_DEVICE", "auto"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        whisper_language=language or None,
        raw_dir=PROJECT_ROOT / "storage" / "raw",
        temp_dir=PROJECT_ROOT / "storage" / "temp",
        output_dir=PROJECT_ROOT / "storage" / "output",
        hook_title_seconds=_float_env("HOOK_TITLE_SECONDS", 4.0),
        hook_title_style=os.getenv("HOOK_TITLE_STYLE", "light").strip().lower() or "light",
        caption_highlight=os.getenv("CAPTION_HIGHLIGHT", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        auto_graphics=os.getenv("AUTO_GRAPHICS", "1").strip().lower() not in {"0", "false", "no", "off"},
        enable_climax_hook=os.getenv("ENABLE_CLIMAX_HOOK", "1").strip().lower() not in {"0", "false", "no", "off"},
        visual_overlays=(os.getenv("VISUAL_OVERLAYS", "executive_minimal").strip().lower() or "executive_minimal"),
        linkedin_posts=os.getenv("LINKEDIN_POSTS", "1").strip().lower() not in {"0", "false", "no", "off"},
    )
    for directory in (settings.raw_dir, settings.temp_dir, settings.output_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings
