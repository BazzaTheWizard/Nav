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
    )
    for directory in (settings.raw_dir, settings.temp_dir, settings.output_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings
