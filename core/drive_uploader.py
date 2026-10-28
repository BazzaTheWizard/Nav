from __future__ import annotations

import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from core.outreach import AGENCY


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET_FILE = PROJECT_ROOT / "client_secret.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"


class DriveAuthError(RuntimeError):
    """Google Drive sign-in is missing or expired; a person has to sign in again."""


def _get_user_credentials(interactive: bool = True) -> Credentials:
    credentials: Credentials | None = None
    if TOKEN_FILE.is_file():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            DRIVE_SCOPES,
        )

    if credentials is not None and credentials.valid:
        return credentials

    if credentials is not None and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise DriveAuthError(
                "Google Drive sign-in expired. Delete token.json, then run the app once "
                "and approve Drive access again. (If this happens every 7 days, switch "
                "your Google Cloud OAuth consent screen from Testing to In production.)"
            ) from exc
        TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
        TOKEN_FILE.chmod(0o600)
        return credentials

    if not interactive:
        raise DriveAuthError(
            "Google Drive isn't signed in yet. Run the app once and approve Drive access."
        )
    if not CLIENT_SECRET_FILE.is_file():
        raise FileNotFoundError(
            f"Google OAuth client secrets were not found at {CLIENT_SECRET_FILE}."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        DRIVE_SCOPES,
    )
    credentials = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
    TOKEN_FILE.chmod(0o600)
    return credentials


def check_drive_access() -> None:
    """Fail fast (DriveAuthError) before any work if Drive can't be used unattended."""
    _get_user_credentials(interactive=False)


UPLOAD_RETRIES = 3  # Google's client retries 5xx/429/network errors with backoff


def upload_clips_to_drive(
    channel_name: str,
    video_title: str,
    clip_paths: list[str],
) -> str:
    parent_folder_id = os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID", "").strip()
    if not parent_folder_id:
        raise RuntimeError("GOOGLE_DRIVE_PARENT_FOLDER_ID is not configured.")
    if not channel_name.strip() or not video_title.strip():
        raise ValueError("A channel name and video title are required for Drive upload.")
    if not clip_paths:
        raise ValueError("At least one rendered clip is required for Drive upload.")

    clips = [Path(clip_path).expanduser().resolve(strict=True) for clip_path in clip_paths]
    for clip_path in clips:
        if not clip_path.is_file():
            raise ValueError(f"Clip path is not a file: {clip_path}")
        if clip_path.suffix.lower() != ".mp4":
            raise ValueError(f"Only MP4 clips can be uploaded: {clip_path.name}")

    credentials = _get_user_credentials()
    drive = build("drive", "v3", credentials=credentials)
    folder = (
        drive.files()
        .create(
            body={
                # Shown at the top of the folder when they open it: who it's from and what it is.
                "name": f"{AGENCY} – {len(clips)} sample Shorts for {channel_name.strip()}",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_folder_id],
            },
            fields="id,webViewLink",
        )
        .execute(num_retries=UPLOAD_RETRIES)
    )
    folder_id = folder.get("id")
    folder_link = folder.get("webViewLink")
    if not isinstance(folder_id, str) or not folder_id:
        raise RuntimeError("Google Drive did not return an ID for the client folder.")

    (
        drive.permissions()
        .create(
            fileId=folder_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        )
        .execute(num_retries=UPLOAD_RETRIES)
    )

    for clip_path in clips:
        media = MediaFileUpload(
            str(clip_path),
            mimetype="video/mp4",
            resumable=True,
        )
        (
            drive.files()
            .create(
                body={
                    "name": clip_path.name,
                    "mimeType": "video/mp4",
                    "parents": [folder_id],
                },
                media_body=media,
                fields="id",
            )
            .execute(num_retries=UPLOAD_RETRIES)
        )

    if not isinstance(folder_link, str) or not folder_link:
        raise RuntimeError("Google Drive did not return a link for the client folder.")
    return folder_link
