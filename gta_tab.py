"""Round 63: the 🎮 GTA tab. Both GTA clipping modes as buttons instead of terminal commands.

- 🎙️ Narrate (tools/narrate.py): footage library -> script -> voiceover -> finished clips
- 📺 Stream clips (tools/clip_stream.py): a licensed campaign VOD -> highlight clips

Each button starts the tool as a background job (core.tool_jobs), with exactly the flags you
would type, so the app and the terminal always make the same clips. The tools set their own
defaults (bold captions, gaming clip length, no LinkedIn copy) in their own process, which keeps
them out of the podcast pipeline that runs in this one. Jobs keep going when you switch tabs or
reload; their output shows live under the buttons, with a Stop button.

The pure helpers (the argv builders, the folder readers) have no Streamlit in them and are tested
in tests/test_round63.py; render_tab() is the UI.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from core import tool_jobs

ROOT = Path(__file__).resolve().parent
NARRATE_TOOL = ROOT / "tools" / "narrate.py"
STREAM_TOOL = ROOT / "tools" / "clip_stream.py"
# The folders the tools use (tools/narrate.py LIBRARY_DB, CAPTION_HISTORY, OUT_DIR;
# tools/clip_stream.py OUT_ROOT). Not imported from the tools: importing them would set their
# environment defaults in the app's process. tests/test_round63.py checks they still match.
NARRATE_DIR = ROOT / "storage" / "narrate"
LIBRARY_DB = NARRATE_DIR / "library.sqlite"
CAPTION_HISTORY = NARRATE_DIR / "caption_styles.json"
SCRIPTS_DIR = NARRATE_DIR / "scripts"
VOICE_DIR = NARRATE_DIR / "voice"
BRIEFS_DIR = NARRATE_DIR / "briefs"
NARRATE_OUT = ROOT / "storage" / "output" / "narrate"
STREAMS_OUT = ROOT / "storage" / "output" / "streams"
CHAT_UPLOADS = ROOT / "storage" / "clip_mode" / "chat"
AUDIO_EXTENSIONS = (".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg")

SCRIPT_FORMATS = {"Tips": "tips", "News": "news", "Details": "details", "Comparison": "comparison"}
FRAMES = {"Tall 4:5 action crop (default)": "tall", "Full-screen 9:16 action crop": "vertical",
          "Whole 16:9 picture": "wide"}
NARRATE_LAYOUTS = {"Blurred background (default)": "blurred_background",
                   "Wide blurred background": "wide_blurred_background", "Smart crop": "smart_crop"}
CAPTION_LOOKS = {"Rotate per video (default)": "rotate", "Bold (yellow word)": "bold",
                 "Neon (cyan glow)": "neon", "Lime (green word)": "lime", "Boxed (pink word on a box)": "boxed"}
STREAM_LAYOUTS = {"Stream: facecam on top, gameplay below (default)": "stream",
                  "Full frame on blur": "blurred_background", "Face-tracked crop": "smart_crop",
                  "Webinar": "webinar", "Automatic": "auto"}
NARRATE, STREAM = "narrate", "stream"


# ------------------------------------------------------------------ argv builders (no UI)
def narrate_ingest_argv(folder: str, no_ai: bool = False) -> list[str]:
    folder = folder.strip()
    if not folder:
        raise ValueError("Give the footage folder (or one video file).")
    if not Path(folder).expanduser().exists():
        raise ValueError(f"Not found: {folder}")
    return [sys.executable, str(NARRATE_TOOL), "ingest", str(Path(folder).expanduser())] + (["--no-ai"] if no_ai else [])


def narrate_script_argv(topic: str, fmt: str, out: Path, brief: Path | None = None) -> list[str]:
    if not topic.strip():
        raise ValueError("Give the script a topic.")
    argv = [sys.executable, str(NARRATE_TOOL), "script", "--topic", topic.strip(), "--format", fmt, "--out", str(out)]
    return argv + (["--brief", str(brief)] if brief else [])


def narrate_render_argv(scripts: list[Path], frame: str = "tall", layout: str = "blurred_background",
                        captions: str = "rotate", game_db: float = -20.0, game_audio: bool = True,
                        allow_reuse: bool = False, voice_dir: Path = VOICE_DIR) -> list[str]:
    if not scripts:
        raise ValueError("Pick at least one script.")
    missing = [s.name for s in scripts if voice_for(s, voice_dir) is None]
    if missing:
        raise ValueError(f"No voiceover yet for: {', '.join(missing)}")
    argv = [sys.executable, str(NARRATE_TOOL), "render", *map(str, scripts), "--voice-dir", str(voice_dir),
            "--frame", frame, "--layout", layout, "--captions", captions, "--game-db", f"{game_db:g}"]
    return argv + (["--no-game-audio"] if not game_audio else []) + (["--allow-reuse"] if allow_reuse else [])


def stream_argv(*, url: str = "", video: str = "", chat: str = "", campaign: str = "", count: int = 6,
                name: str = "", streamer: str = "", game: str = "", layout: str = "stream",
                audio_signal: bool = True, judge: bool = True, plan: bool = False) -> list[str]:
    url, video, campaign = url.strip(), video.strip(), campaign.strip()
    if not campaign:
        raise ValueError("Name the clipping campaign that licenses this VOD (e.g. \"Whop: <campaign>\").")
    if not url and not video:
        raise ValueError("Give a Twitch / YouTube VOD link, or the path of a VOD on this Mac.")
    if url and not re.match(r"https?://", url):
        raise ValueError("The VOD link should start with https://")
    if video and not Path(video).expanduser().is_file():
        raise ValueError(f"No video at {video}")
    if chat.strip() and not Path(chat.strip()).expanduser().is_file():
        raise ValueError(f"No chat file at {chat}")
    argv = [sys.executable, str(STREAM_TOOL)] + ([url] if url else [])
    argv += ["--campaign", campaign, "--count", str(int(count)), "--layout", layout]
    for flag, value in (("--video", str(Path(video).expanduser()) if video else ""),
                        ("--chat", str(Path(chat.strip()).expanduser()) if chat.strip() else ""),
                        ("--name", slug(name) if name.strip() else ""), ("--streamer", streamer.strip()),
                        ("--game", game.strip())):
        if value:
            argv += [flag, value]
    argv += (["--no-audio-signal"] if not audio_signal else []) + (["--no-judge"] if not judge else [])
    return argv + (["--plan"] if plan else [])


# ------------------------------------------------------------------ folder readers (no UI)
def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def voice_for(script: Path, voice_dir: Path = VOICE_DIR) -> Path | None:
    for ext in AUDIO_EXTENSIONS:
        candidate = voice_dir / (script.stem + ext)
        if candidate.is_file():
            return candidate
    return None


def scripts(scripts_dir: Path = SCRIPTS_DIR) -> list[Path]:
    return sorted(scripts_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True) if scripts_dir.is_dir() else []


def final_clip(script: Path, out_dir: Path = NARRATE_OUT) -> Path | None:
    made = out_dir / f"{script.stem}_final.mp4"
    return made if made.is_file() else None


def library_summary(db: Path = LIBRARY_DB, top: int = 12) -> dict[str, Any]:
    """Shots, minutes and the commonest tags, read straight from the library (read-only)."""
    if not db.is_file():
        return {"shots": 0, "minutes": 0.0, "tags": [], "renders": 0}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT start, end, tags FROM shots").fetchall()
        renders = con.execute("SELECT COUNT(*) FROM renders").fetchone()[0]
    except sqlite3.Error:
        return {"shots": 0, "minutes": 0.0, "tags": [], "renders": 0}
    finally:
        con.close()
    tags = Counter(t for _, _, raw in rows for t in json.loads(raw or "[]"))
    return {"shots": len(rows), "minutes": sum(max(0.0, b - a) for a, b, _ in rows) / 60, "tags": tags.most_common(top),
            "renders": renders}


def caption_looks(history: Path = CAPTION_HISTORY) -> dict[str, str]:
    try:
        return dict(json.loads(history.read_text()).get("assigned") or {})
    except (OSError, ValueError, AttributeError):
        return {}


def stream_runs(out_root: Path = STREAMS_OUT) -> list[Path]:
    """Clip-mode output folders, newest first (only ones with something in them)."""
    if not out_root.is_dir():
        return []
    runs = [d for d in out_root.iterdir() if d.is_dir() and any((d / f).is_file() for f in
                                                                 ("moments.json", "picks.json", "heatmap.json"))]
    return sorted(runs, key=lambda d: max(f.stat().st_mtime for f in d.iterdir()), reverse=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_upload(data: bytes, folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def save_voice(script: Path, data: bytes, suffix: str, voice_dir: Path = VOICE_DIR) -> Path:
    """The voiceover for ``script``, named like it (replacing an earlier take in another format)."""
    suffix = suffix.lower() if suffix.lower() in AUDIO_EXTENSIONS else ".m4a"
    voice_dir.mkdir(parents=True, exist_ok=True)
    for ext in AUDIO_EXTENSIONS:
        old = voice_dir / (script.stem + ext)
        if ext != suffix and old.is_file():
            old.unlink()
    return save_upload(data, voice_dir, script.stem + suffix)


def new_script_path(name: str, scripts_dir: Path = SCRIPTS_DIR) -> Path:
    stem = slug(name) or time.strftime("script-%Y%m%d-%H%M")
    path, n = scripts_dir / f"{stem}.txt", 2
    while path.exists():
        path, n = scripts_dir / f"{stem}-{n}.txt", n + 1
    return path


# ------------------------------------------------------------------ UI
def render_tab() -> None:
    import streamlit as st

    st.caption("Both GTA modes, as buttons. Each one runs the same command-line tool in the background "
               "(you can switch tabs while it works) and shows its progress here.")
    mode = st.radio("GTA mode", ["🎙️ Narrate", "📺 Stream clips"], key="gta_mode", horizontal=True,
                    label_visibility="collapsed",
                    help="Narrate: your script and voiceover over bought gameplay. Stream clips: highlights "
                    "from a streamer's VOD, only for a clipping campaign you've joined.")
    if mode.endswith("Narrate"):
        _render_narrate(st)
    else:
        _render_stream(st)


def _start(st: Any, kind: str, label: str, argv: list[str]) -> None:
    busy = tool_jobs.running(kind)
    if busy:
        st.warning(f"Still running: {busy[0].label}. Wait for it or stop it first.")
        return
    tool_jobs.start(kind, label, argv)
    st.rerun()


def _render_job(st: Any, kind: str) -> None:
    """The newest job of this kind: live output while it runs, then how it ended."""
    found = tool_jobs.jobs(kind)
    if not found:
        return
    job = found[0]

    def panel() -> None:
        status = job.status()
        icon = {"running": "⏳", "done": "✅", "failed": "❌", "stopped": "⏹️"}[status]
        minutes, seconds = divmod(int(job.seconds()), 60)
        head, stop_column = st.columns([5, 1], vertical_alignment="center")
        head.markdown(f"{icon} **{job.label}** · {status} · {minutes}:{seconds:02d}")
        if status == "running" and stop_column.button("Stop", key=f"stop_{job.id}"):
            tool_jobs.stop(job)
            st.rerun()
        st.code(job.tail(30) or "(starting…)", language=None)
        if status != "running" and st.session_state.get(f"job_seen_{job.id}") == "running":
            st.session_state[f"job_seen_{job.id}"] = status
            st.rerun()  # the job just ended: refresh the whole tab so its clips show
        st.session_state[f"job_seen_{job.id}"] = status

    fragment = getattr(st, "fragment", None)
    if job.status() == "running" and fragment is not None:
        fragment(run_every=2)(panel)()
    else:
        panel()
        if job.status() == "running":
            st.button("Refresh", key=f"refresh_{kind}")


# --- Narrate ---------------------------------------------------------------------------
def _render_narrate(st: Any) -> None:
    _render_job(st, NARRATE)
    lib = library_summary()
    st.subheader("1 · Footage library")
    if lib["shots"]:
        st.caption(f"{lib['shots']} shots · {lib['minutes']:.1f} minutes · {lib['renders']} videos made · "
                   + ", ".join(f"{t} {n}" for t, n in lib["tags"]))
    else:
        st.info("The library is empty: add a folder of gameplay first. Folder and file names become tags, "
                "so name folders by what's in them (beach_sunset/, city_night_driving/).")
    with st.form("narrate_ingest"):
        folder = st.text_input("Footage folder (or one video) on this Mac", placeholder="/Users/you/Footage/gta6")
        no_ai = st.checkbox("Skip AI scene tags (Gemini)", help="Names, brightness and motion only. Faster, and no "
                            "GEMINI_API_KEY needed.")
        if st.form_submit_button("Add to library"):
            try:
                _start(st, NARRATE, f"Add footage: {Path(folder).name}", narrate_ingest_argv(folder, no_ai))
            except ValueError as exc:
                st.error(str(exc))

    st.subheader("2 · Script")
    how = st.radio("How", ["✍️ Write with AI", "📋 Paste your own"], key="narrate_script_how", horizontal=True,
                   label_visibility="collapsed")
    if how.startswith("✍️"):
        _render_write_form(st)
    else:
        _render_paste_form(st)
    _render_scripts(st)


def _render_write_form(st: Any) -> None:
    with st.form("narrate_write"):
        topic = st.text_input("Topic", placeholder="Don't sell cars at the first garage")
        fmt_column, name_column = st.columns(2)
        fmt = fmt_column.selectbox("Format", list(SCRIPT_FORMATS))
        name = name_column.text_input("File name (optional)", placeholder="From the topic")
        brief = st.text_area("Facts and source links it may use (optional)", height=110)
        if st.form_submit_button("Write script"):
            try:
                out = new_script_path(name or topic)
                brief_path = save_upload(brief.encode(), BRIEFS_DIR, out.stem + ".txt") if brief.strip() else None
                SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
                _start(st, NARRATE, f"Write script: {out.stem}",
                       narrate_script_argv(topic, SCRIPT_FORMATS[fmt], out, brief_path))
            except ValueError as exc:
                st.error(str(exc))


def _render_paste_form(st: Any) -> None:
    with st.form("narrate_paste"):
        name = st.text_input("File name", placeholder="garage-tip")
        text = st.text_area("Script", height=180, placeholder='HOOK: You\'ve been doing this wrong since day one.\n'
                            '[driving, city, night] Most players never...\n[TEXT: "SAVE $40K"] [ZOOM] Here\'s the trick...')
        if st.form_submit_button("Save script"):
            if not text.strip():
                st.error("Paste the script first.")
            else:
                path = new_script_path(name or text.split("\n", 1)[0])
                save_upload(text.strip().encode() + b"\n", SCRIPTS_DIR, path.name)
                st.session_state["narrate_script_pick"] = path.name
                st.success(f"Saved {path.name}")


def _render_scripts(st: Any) -> None:
    found = scripts()
    if not found:
        st.caption("No scripts yet.")
        return
    st.subheader("3 · Record and check")
    names = [p.name for p in found]
    if st.session_state.get("narrate_script_pick") not in names:
        st.session_state["narrate_script_pick"] = names[0]
    picked = SCRIPTS_DIR / st.selectbox("Script", names, key="narrate_script_pick", format_func=lambda n: (
        n + ("  · 🎤" if voice_for(SCRIPTS_DIR / n) else "  · no voiceover")
        + ("  · ✅ made" if final_clip(SCRIPTS_DIR / n) else "")))
    _render_script(st, picked)

    st.subheader("4 · Render")
    ready = [p for p in found if voice_for(p)]
    if not ready:
        st.caption("Upload a voiceover for a script to render it.")
    else:
        defaults = [p.name for p in ready if not final_clip(p)][:5] or [ready[0].name]
        with st.form("narrate_render"):
            chosen = st.multiselect("Scripts to render", [p.name for p in ready], default=defaults)
            frame_column, look_column = st.columns(2)
            frame = frame_column.selectbox("Frame", list(FRAMES))
            look = look_column.selectbox(
                "Caption look", list(CAPTION_LOOKS),
                help="Rotate: each video gets the look used least recently (and never one the same colour "
                "as its pop-ups), and keeps it when re-rendered. Or fix one look for this batch.")
            layout_column, audio_column = st.columns(2)
            layout = layout_column.selectbox("Layout", list(NARRATE_LAYOUTS))
            game_db = audio_column.slider("Game sound under your voice (dB)", -40, -6, -20)
            game_audio = st.checkbox("Keep the game sound", value=True)
            allow_reuse = st.checkbox("Allow heavy shot reuse", help="Normally a render stops when over 40% of its "
                                      "shots were in the last 10 videos.")
            if st.form_submit_button("Render", type="primary"):
                try:
                    paths = [SCRIPTS_DIR / n for n in chosen]
                    _start(st, NARRATE, f"Render {len(paths)} clip{'s' * (len(paths) != 1)}",
                           narrate_render_argv(paths, FRAMES[frame], NARRATE_LAYOUTS[layout], CAPTION_LOOKS[look],
                                               game_db, game_audio, allow_reuse))
                except ValueError as exc:
                    st.error(str(exc))
    _render_narrate_clips(st, found)


def _render_script(st: Any, path: Path) -> None:
    from core.narrate import script as S

    text = path.read_text(errors="replace")
    try:
        parsed = S.parse(text)
        problems = S.check(parsed)
    except Exception as exc:  # noqa: BLE001 - a half-written script is normal here
        parsed, problems = None, [f"Could not read it: {exc}"]
    for problem in problems:
        st.warning(problem)
    if parsed is not None:
        with st.expander(f"🎤 Read this aloud ({parsed.word_count()} words, about "
                         f"{parsed.word_count() / 2.6:.0f} s)", expanded=voice_for(path) is None):
            st.markdown("\n\n".join(f"<p style='font-size:1.55rem;line-height:1.45;margin:0'>{html.escape(line.text)}</p>"
                                    for line in parsed.lines), unsafe_allow_html=True)
    with st.expander("✏️ Edit the script"):
        edited = st.text_area("Script text", text, height=260, key=f"edit_{path.name}", label_visibility="collapsed")
        if st.button("Save changes", key=f"save_{path.name}") and edited != text:
            path.write_text(edited.rstrip() + "\n")
            st.rerun()
    voice = voice_for(path)
    if voice:
        st.audio(str(voice))
    upload = st.file_uploader("Voiceover for this script" + (" (replaces the one above)" if voice else ""),
                              type=[e.lstrip(".") for e in AUDIO_EXTENSIONS], key=f"voice_{path.name}")
    if upload is not None and st.session_state.get(f"voice_saved_{path.name}") != upload.file_id:
        save_voice(path, upload.getvalue(), Path(upload.name).suffix)
        st.session_state[f"voice_saved_{path.name}"] = upload.file_id
        st.rerun()


def _render_narrate_clips(st: Any, found: list[Path]) -> None:
    made = [(p, final_clip(p)) for p in found]
    made = sorted([(p, c) for p, c in made if c], key=lambda pc: pc[1].stat().st_mtime, reverse=True)[:6]
    if not made:
        return
    st.subheader("Finished clips")
    looks = caption_looks()
    columns = st.columns(3)
    for n, (script, clip) in enumerate(made):
        with columns[n % 3]:
            st.video(str(clip))
            st.caption(f"{script.stem} · {looks.get(script.stem, '?')} captions · {clip}")


# --- Stream clips ----------------------------------------------------------------------
def _render_stream(st: Any) -> None:
    st.caption("Only clip streamers who run a clipping campaign you've joined: the campaign is saved with the "
               "clips, so you always know which licence a clip was made under.")
    _render_job(st, STREAM)
    with st.form("stream_clip"):
        url = st.text_input("Twitch / YouTube VOD link", placeholder="https://www.twitch.tv/videos/123456789")
        video = st.text_input("Or a VOD on this Mac (its path)", placeholder="/Users/you/Movies/vod.mp4",
                              help="Skips the downloads.")
        campaign = st.text_input("Clipping campaign (required)", placeholder="Whop: <campaign name>")
        count_column, layout_column = st.columns([2, 3])
        count = count_column.slider("Clips", 1, 12, 6)
        layout = layout_column.selectbox("Layout", list(STREAM_LAYOUTS),
                                         help="Stream falls back to the blurred layout when no facecam is found.")
        with st.expander("More (optional)"):
            chat_path = st.text_input("Chat replay file on this Mac",
                                      help="TwitchDownloader JSON, yt-dlp live_chat, or a CSV of seconds,message. "
                                      "Without it the tool downloads the chat itself when it can.")
            chat_upload = st.file_uploader("Or upload the chat file", type=["json", "csv", "jsonl"])
            name_column, streamer_column, game_column = st.columns(3)
            name = name_column.text_input("Folder name", placeholder="From the link")
            streamer = streamer_column.text_input("Streamer", placeholder="From the link")
            game = game_column.text_input("Game", placeholder="From the link")
            audio_signal = st.checkbox("Use loudness spikes", value=True, help="Off: rank by chat only.")
            judge = st.checkbox("Judge each clip cold (one more model call per clip)", value=True)
        plan_column, make_column = st.columns(2)
        plan = plan_column.form_submit_button("🔍 Find moments (dry run)",
                                              help="No model calls, no rendering: shows the moments it would use.")
        make = make_column.form_submit_button("🎬 Make clips", type="primary")
    if plan or make:
        try:
            chat = chat_path
            if chat_upload is not None:
                chat = str(save_upload(chat_upload.getvalue(), CHAT_UPLOADS,
                                       time.strftime("%Y%m%d-%H%M%S-") + Path(chat_upload.name).name))
            argv = stream_argv(url=url, video=video, chat=chat, campaign=campaign, count=count, name=name,
                               streamer=streamer, game=game, layout=STREAM_LAYOUTS[layout],
                               audio_signal=audio_signal, judge=judge, plan=plan)
            what = url.strip().rsplit("/", 1)[-1] if url.strip() else Path(video).name
            _start(st, STREAM, f"{'Find moments' if plan else 'Make clips'}: {what}", argv)
        except ValueError as exc:
            st.error(str(exc))
    _render_stream_results(st)


def _render_stream_results(st: Any) -> None:
    runs = stream_runs()
    if not runs:
        return
    st.subheader("Results")
    run = st.selectbox("Stream", runs, format_func=lambda d: d.name, key="stream_run")
    picks = read_json(run / "picks.json") or {}
    if picks:
        st.caption(f"Campaign: **{picks.get('campaign', '?')}** · {picks.get('streamer') or '?'} / "
                   f"{picks.get('game') or '?'} · {picks.get('source', '')}")
    moments = (read_json(run / "moments.json") or {}).get("moments") or []
    if moments:
        with st.expander(f"Moments considered ({len(moments)})", expanded=not picks):
            st.dataframe([{"start": round(m["start"], 1), "end": round(m["end"], 1), "label": m.get("label"),
                           "score": m.get("score"), "judge": m.get("judge"), "title": m.get("title"),
                           "why": m.get("why")} for m in moments], hide_index=True)
    clips = [c for c in picks.get("clips") or [] if Path(c.get("file", "")).is_file()]
    columns = st.columns(3)
    for n, clip in enumerate(clips):
        with columns[n % 3]:
            st.video(clip["file"])
            st.caption(f"{clip['start']:.0f}-{clip['end']:.0f} s · score {clip.get('score')} · {clip.get('title', '')}")
