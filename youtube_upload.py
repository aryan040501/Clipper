"""YouTube upload for clip-watcher.

Uses the same OAuth "installed app" flow as the Youtube Automation project
(client_secrets.json + a saved token), with two changes: the token is stored as
JSON instead of a pickle, and an expired token is refreshed instead of sending
you through the browser again. The Google libraries are imported lazily, so
clip-watcher still runs without them when --youtube is not used.
"""
from __future__ import annotations

import collections
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
SHORTS_MAX_SECONDS = 180

# Chat words that count as a "reaction" even though they are not CamelCase/ALL-CAPS emotes.
REACTION_WORDS = {"lol", "lmao", "lmfao", "omg", "wtf", "pog", "gg", "rip", "clip", "insane", "cracked"}
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class UploadJob:
    path: Path
    title: str
    description: str
    tags: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- metadata


def top_reactions(texts: Sequence[str], n: int = 3) -> List[str]:
    """The emotes / reaction words used by the most messages. A word is counted
    once per message, so one person spamming 'KEKW KEKW KEKW' does not win."""
    counts: collections.Counter = collections.Counter()
    for text in texts:
        words = set()
        for word in WORD_RE.findall(text):
            emote_like = word.isupper() or any(c.isupper() for c in word[1:])
            if emote_like or word.lower() in REACTION_WORDS:
                words.add(word.upper() if word.lower() in REACTION_WORDS else word)
        counts.update(words)
    return [word for word, _ in counts.most_common(n)]


def _fit(base: str, suffix: str, limit: int) -> str:
    room = limit - len(suffix)
    if len(base) > room:
        base = base[: max(room - 1, 0)].rstrip() + "…"
    return base + suffix


def build_metadata(
    channel: str,
    peak: int,
    duration: float,
    spike_time: float,
    chat_texts: Sequence[str],
    chatters: int,
    bucket: float = 10,
) -> UploadJob:
    """Title, description and tags for a clip, built only from what we know:
    the channel, how hard chat reacted, and which emotes chat used."""
    reactions = top_reactions(chat_texts)
    shorts = duration <= SHORTS_MAX_SECONDS
    when = time.strftime("%b %d, %Y", time.localtime(spike_time))

    if reactions:
        base = f"{channel} | Chat goes wild ({', '.join(reactions[:2])})"
    else:
        base = f"{channel} | Chat goes wild"
    title = _fit(base, " #Shorts" if shorts else "", TITLE_MAX)

    lines = [
        f"Chat exploded during {channel}'s stream on {when}: {peak} messages in {bucket:g} seconds"
        + (f" from {chatters} chatters." if chatters else "."),
    ]
    if reactions:
        lines.append(f"Top reactions: {' '.join(reactions)}")
    lines += [
        "",
        f"Clip from https://twitch.tv/{channel} (all credit to the streamer; go watch them live).",
        "",
        " ".join(f"#{t}" for t in ([channel, "twitch", "twitchclips"] + (["shorts"] if shorts else []))),
    ]
    description = "\n".join(lines)[:DESCRIPTION_MAX].replace("<", "").replace(">", "")

    tags = [channel, f"{channel} clips", "twitch", "twitch clips", "stream highlights", "funny moments"]
    tags += [r.lower() for r in reactions]
    return UploadJob(Path(), title, description, tags)


# -------------------------------------------------------------------- auth


def authenticate(secrets_file: Path, token_file: Path):
    """Return an authorised YouTube API client, opening the browser only the
    first time (or when the saved token can no longer be refreshed)."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # revoked or expired for good: sign in again
    if not creds or not creds.valid:
        if not secrets_file.exists():
            raise FileNotFoundError(
                f"{secrets_file} not found. Download the OAuth client file from Google Cloud "
                "(Desktop app) and put it there, or pass --youtube-secrets."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), SCOPES)
        creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


# ------------------------------------------------------------------ upload


def upload_video(youtube, job: UploadJob, category_id: str, privacy: str) -> str:
    """Upload one file and return the video id."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": job.title,
            "description": job.description,
            "tags": job.tags,
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(job.path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    failures = 0
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as err:
            if err.resp.status in (500, 502, 503, 504) and failures < 5:
                failures += 1
                time.sleep(2 ** failures)
                continue
            raise
        if status:
            log(f"youtube: {job.path.name} {int(status.progress() * 100)}%")
    return response["id"]


class YouTubeUploader(threading.Thread):
    """Uploads clips one at a time in the background, so cutting and chat
    reading never wait on the network."""

    def __init__(
        self,
        youtube,
        category_id: str = "20",
        privacy: str = "private",
        uploader: Callable = upload_video,
    ) -> None:
        super().__init__(name="youtube-uploader")
        self.youtube = youtube
        self.category_id = category_id
        self.privacy = privacy
        self._upload = uploader
        self.jobs: "queue.Queue[Optional[UploadJob]]" = queue.Queue()

    def submit(self, job: UploadJob) -> None:
        self.jobs.put(job)

    def finish(self) -> None:
        """Wait for queued uploads, then stop."""
        self.jobs.put(None)
        self.join()

    def run(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                video_id = self._upload(self.youtube, job, self.category_id, self.privacy)
                log(f"youtube: uploaded {job.path.name} -> https://youtu.be/{video_id} ({self.privacy})")
            except Exception as err:  # keep going; one failed upload must not stop the rest
                log(f"youtube: upload of {job.path.name} failed: {err}")
