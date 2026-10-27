from __future__ import annotations

import argparse
import logging
import math
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import cv2
from tqdm import tqdm

from config import load_settings
from core.downloader import (
    download_audio_only,
    download_source_video,
    extract_audio_only,
    extract_audio_segment,
)
from core.face_tracker import analyze_vertical_crop
from core.hook_selector import select_clips
from core.storage_manager import (
    prune_output_folder,
    prune_stale_files,
    render_output_dir,
)
from core.subtitle_engine import create_ass_subtitles
from core.transcriber import (
    Word,
    clip_words_from_transcript,
    transcribe_audio,
    transcribe_clip_audio,
    transcribe_youtube_captions,
    youtube_video_id,
)
from core.video_renderer import LAYOUT_MODES, render_clip


def _video_duration(video_path: Path) -> float:
    # The container duration from ffprobe is exact; OpenCV's frame count is only
    # an estimate and is often wrong for variable-frame-rate files.
    try:
        return _audio_duration(Path(video_path))
    except RuntimeError:
        pass
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Could not read video metadata: {video_path}")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or frame_count <= 0:
            raise RuntimeError(f"Video has invalid duration metadata: {video_path}")
        return frame_count / fps
    finally:
        capture.release()


def _audio_duration(audio_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("FFprobe was not found. Install FFmpeg and ensure it is on your PATH.")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        duration = float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(f"Could not read audio duration: {detail.strip()}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Audio has invalid duration metadata: {audio_path}")
    return duration


def _words_for_clip(words: list[Word], start: float, end: float) -> list[Word]:
    clip_words = []
    for word in words:
        if word.end > start and word.start < end:
            relative_start = max(0.0, word.start - start)
            relative_end = min(end - start, word.end - start)
            if relative_end > relative_start:
                clip_words.append(
                    Word(text=word.text, start=relative_start, end=relative_end)
                )
    if not clip_words:
        raise RuntimeError(
            f"No transcribed words overlap the selected clip at {start:.2f}-{end:.2f}s."
        )
    return clip_words


def run_pipeline(
    input_path: str,
    clips_count: int,
    layout_mode: str = "blurred_background",
) -> list[Path]:
    settings = load_settings()
    output_dir = render_output_dir()
    prune_output_folder(output_dir=output_dir)
    prune_stale_files(settings.raw_dir)
    prune_stale_files(settings.temp_dir)
    is_url = input_path.startswith(("http://", "https://"))
    source_video = None if is_url else Path(input_path).expanduser().resolve(strict=True)
    downloaded_source: Path | None = None
    audio_path = settings.temp_dir / f"audio_{uuid.uuid4().hex}.wav"
    try:
        video_id = youtube_video_id(input_path) if is_url else None
        words = transcribe_youtube_captions(video_id) if video_id else None
        if words:
            duration = max(word.end for word in words)
            logging.info("Using YouTube captions for %s", input_path)
        else:
            if is_url:
                download_audio_only(input_path, audio_path)
                duration = _audio_duration(audio_path)
            else:
                if (
                    source_video is None
                    or source_video.suffix.lower() not in {".mp4", ".mov"}
                ):
                    raise ValueError("Expected a local .mp4 or .mov input.")
                extract_audio_only(source_video, audio_path)
                duration = _video_duration(source_video)

            logging.info("Transcribing audio for %s", input_path)
            words = transcribe_audio(audio_path, settings)
        logging.info("Selecting up to %d clips", clips_count)
        clips = select_clips(words, duration, clips_count, settings)
        audio_path.unlink(missing_ok=True)

        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]

        if is_url and clips:
            # Download the source once and reuse it for every clip. The old code
            # downloaded the full video again for each clip.
            logging.info("Downloading source video for rendering")
            downloaded_source = Path(download_source_video(input_path))
            source_video = downloaded_source

        rendered: list[Path] = []
        for index, clip in enumerate(tqdm(clips, desc="Rendering clips"), start=1):
            output_path = output_dir / f"clip_{run_id}_{index:02d}.mp4"
            subtitle_path = settings.temp_dir / f"{output_path.stem}.ass"
            clip_audio_path = settings.temp_dir / f"{output_path.stem}.wav"
            video_path = source_video
            video_start = clip.start_time
            video_end = clip.end_time
            rendered_successfully = False
            try:
                if video_path is None:
                    raise RuntimeError("No source video is available for rendering.")
                # Reuse word timings from the full transcript when it has them;
                # only re-transcribe when it came from caption lines.
                clip_words = clip_words_from_transcript(words, video_start, video_end)
                if clip_words is None:
                    extract_audio_segment(
                        video_path,
                        video_start,
                        video_end,
                        clip_audio_path,
                    )
                    clip_words = transcribe_clip_audio(clip_audio_path, settings)
                create_ass_subtitles(
                    clip_words,
                    subtitle_path,
                    clip_duration=video_end - video_start,
                )
                crop_analysis = analyze_vertical_crop(video_path, video_start, video_end)
                render_layout = (
                    "wide_blurred_background"
                    if crop_analysis.use_wide_layout
                    else layout_mode
                )
                rendered_path = render_clip(
                    video_path,
                    subtitle_path,
                    crop_analysis.crop,
                    video_start,
                    video_end,
                    output_path,
                    layout_mode=render_layout,
                )
                rendered.append(rendered_path)
                rendered_successfully = True
            finally:
                clip_audio_path.unlink(missing_ok=True)
                subtitle_path.unlink(missing_ok=True)
                if not rendered_successfully:
                    output_path.unlink(missing_ok=True)
            logging.info(
                "Rendered %s — %s (score %d)",
                output_path,
                clip.viral_title,
                clip.virality_score,
            )
        return rendered
    finally:
        audio_path.unlink(missing_ok=True)
        if downloaded_source is not None:
            downloaded_source.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repurpose a local MP4/MOV or YouTube URL into subtitled vertical clips."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a local .mp4/.mov file or an http(s) video URL.",
    )
    parser.add_argument(
        "--clips",
        type=int,
        default=3,
        help="Maximum number of 30-60 second clips to render (default: 3).",
    )
    parser.add_argument(
        "--layout",
        choices=LAYOUT_MODES,
        default="blurred_background",
        help="Vertical framing layout (default: blurred_background).",
    )
    args = parser.parse_args()
    if args.clips < 1:
        parser.error("--clips must be at least 1.")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    rendered = run_pipeline(args.input, args.clips, args.layout)
    print(f"\nFinished. Rendered {len(rendered)} clip(s):")
    for path in rendered:
        print(f"  {path}")


if __name__ == "__main__":
    main()
