#!/usr/bin/env python3
"""clip-watcher: record a Twitch stream and auto-cut clips when chat spikes.

No AI and no API keys. streamlink records the stream, chat is read anonymously
over IRC, and ffmpeg cuts a 9:16 clip around every chat spike. Each clip also
gets a .srt file with the chat messages that were sent during it.
"""
from __future__ import annotations

import argparse
import collections
import queue
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional, Tuple

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667
IRC_NICK = "justinfan12345"  # anonymous read-only login, no token needed

PRIVMSG_RE = re.compile(r"^(?:@\S+ )?:(?P<user>[^!\s]+)!\S+ PRIVMSG #\S+ :(?P<text>.*)$")

ROOT = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class ChatMessage:
    time: float  # wall-clock time the message was received
    user: str
    text: str


@dataclass
class Clip:
    spike_time: float  # start of the first spike bucket
    wall_start: float
    wall_end: float
    peak: int  # highest message count of the spike buckets it covers


def parse_privmsg(line: str) -> Optional[Tuple[str, str]]:
    match = PRIVMSG_RE.match(line)
    if not match:
        return None
    return match.group("user"), match.group("text")


class TwitchChat:
    """Anonymous, read-only Twitch chat over plain IRC."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.sock: Optional[socket.socket] = None
        self.buf = b""

    def connect(self) -> None:
        self.close()
        self.sock = socket.create_connection((IRC_HOST, IRC_PORT), timeout=10)
        self.sock.settimeout(0.5)
        self.buf = b""
        self._send(f"NICK {IRC_NICK}")
        self._send(f"JOIN #{self.channel}")
        log(f"chat: joined #{self.channel} as {IRC_NICK}")

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _send(self, line: str) -> None:
        assert self.sock
        self.sock.sendall((line + "\r\n").encode("utf-8"))

    def poll(self) -> List[Tuple[str, str]]:
        """Return the (user, text) pairs received in the next ~0.5 s."""
        assert self.sock
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return []
        if not data:
            raise ConnectionError("chat connection closed")
        self.buf += data
        *lines, self.buf = self.buf.split(b"\r\n")
        messages = []
        for raw in lines:
            line = raw.decode("utf-8", errors="replace")
            if line.startswith("PING"):
                self._send("PONG" + line[4:])
            elif " RECONNECT" in line:
                raise ConnectionError("server asked us to reconnect")
            else:
                parsed = parse_privmsg(line)
                if parsed:
                    messages.append(parsed)
        return messages


class BucketCounter:
    """Counts messages in fixed-size, wall-clock-aligned time buckets."""

    def __init__(self, size: float, now: float) -> None:
        self.size = size
        self.bucket_start = now - now % size
        self.count = 0

    def tick(self, now: float) -> List[Tuple[float, int]]:
        """Close every bucket that ended before `now`; return (start, count) pairs."""
        closed = []
        while now >= self.bucket_start + self.size:
            closed.append((self.bucket_start, self.count))
            self.bucket_start += self.size
            self.count = 0
        return closed

    def add(self, n: int = 1) -> None:
        self.count += n


class SpikeDetector:
    """A bucket is a spike when it has >= factor x the median of the recent
    buckets and at least min_messages."""

    def __init__(self, window_buckets: int, factor: float, min_messages: int, warmup_buckets: int) -> None:
        self.history: Deque[int] = collections.deque(maxlen=window_buckets)
        self.factor = factor
        self.min_messages = min_messages
        self.warmup_buckets = warmup_buckets
        self.baseline = 0.0

    def add(self, count: int) -> bool:
        is_spike = False
        if len(self.history) >= self.warmup_buckets:
            self.baseline = statistics.median(self.history)
            is_spike = count >= self.min_messages and count >= self.factor * self.baseline
        self.history.append(count)
        return is_spike


class ClipPlanner:
    """Turns spikes into clip windows, merging spikes that overlap a clip
    that has not been cut yet."""

    def __init__(self, before: float, after: float, max_len: float, wait: float) -> None:
        self.before = before
        self.after = after
        self.max_len = max_len
        self.wait = wait  # how long after a clip's end to wait before cutting it
        self.pending: List[Clip] = []
        self.last_cut_end = float("-inf")

    def on_spike(self, t: float, count: int) -> Optional[Clip]:
        """Register a spike at bucket start `t`. Returns the new clip, or None
        when the spike was merged into a pending one."""
        start, end = t - self.before, t + self.after
        if self.pending and start <= self.pending[-1].wall_end:
            clip = self.pending[-1]
            clip.wall_end = min(max(clip.wall_end, end), clip.wall_start + self.max_len)
            clip.peak = max(clip.peak, count)
            return None
        # Don't repeat footage that is already in a clip that was cut.
        clip = Clip(spike_time=t, wall_start=max(start, self.last_cut_end), wall_end=end, peak=count)
        self.pending.append(clip)
        return clip

    def due(self, now: float) -> List[Clip]:
        ready = [c for c in self.pending if c.wall_end + self.wait <= now]
        return self._take(ready)

    def drain(self) -> List[Clip]:
        return self._take(list(self.pending))

    def _take(self, clips: List[Clip]) -> List[Clip]:
        for clip in clips:
            self.pending.remove(clip)
            self.last_cut_end = max(self.last_cut_end, clip.wall_end)
        return clips


def srt_time(seconds: float) -> str:
    ms = int(round(max(seconds, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_chat_srt(
    messages: List[ChatMessage], clip_start: float, clip_end: float, hold: float = 4.0, max_lines: int = 3
) -> str:
    """Chat as subtitles: each cue shows the newest messages (up to max_lines,
    none older than `hold` seconds) until the next message arrives."""
    msgs = [m for m in messages if clip_start <= m.time < clip_end]
    cues = []
    for i, msg in enumerate(msgs):
        start = msg.time - clip_start
        end = min(start + hold, clip_end - clip_start)
        if i + 1 < len(msgs):
            end = min(end, msgs[i + 1].time - clip_start)
        if end - start < 0.05:
            continue  # the next cue already shows this message
        visible = [m for m in msgs[: i + 1] if msg.time - m.time < hold][-max_lines:]
        text = "\n".join(f"{m.user}: {m.text[:80]}" for m in visible)
        cues.append(f"{len(cues) + 1}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
    return "\n".join(cues)


def cut_clip(rec_path: Path, start: float, duration: float, out_path: Path) -> bool:
    """Cut [start, start + duration] out of the recording, center-cropped to 9:16."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.2f}", "-i", str(rec_path), "-t", f"{duration:.2f}",
        "-vf", "crop=trunc(ih*9/16/2)*2:ih",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return subprocess.run(cmd).returncode == 0


class Recorder:
    def __init__(self, channel: str, rec_dir: Path, quality: str) -> None:
        rec_dir.mkdir(parents=True, exist_ok=True)
        self.path = rec_dir / f"{channel}_{time.strftime('%Y%m%d-%H%M%S')}.ts"
        self.cmd = ["streamlink", f"https://twitch.tv/{channel}", quality, "-o", str(self.path)]
        self.proc: Optional[subprocess.Popen] = None
        self.started_at = 0.0  # wall-clock time of offset 0 in the recording

    def start(self, timeout: float = 60.0) -> None:
        # Own session, so Ctrl+C reaches us first and we stop streamlink ourselves.
        self.proc = subprocess.Popen(self.cmd, start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("streamlink exited before recording started (is the channel live?)")
            if self.path.exists() and self.path.stat().st_size > 0:
                self.started_at = time.time()
                log(f"recording to {self.path}")
                return
            time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"no data from streamlink after {timeout:.0f} s")

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.running():
            assert self.proc
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class ClipWorker(threading.Thread):
    """Runs ffmpeg off the main thread so reading chat never stalls."""

    def __init__(self, args: argparse.Namespace, recorder: Recorder) -> None:
        super().__init__(name="clip-worker")
        self.args = args
        self.recorder = recorder
        self.jobs: "queue.Queue[Optional[Tuple[Clip, List[ChatMessage]]]]" = queue.Queue()
        args.clips_dir.mkdir(parents=True, exist_ok=True)

    def submit(self, clip: Clip, messages: List[ChatMessage]) -> None:
        self.jobs.put((clip, messages))

    def finish(self) -> None:
        self.jobs.put(None)
        self.join()

    def run(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            self.process(*job)

    def process(self, clip: Clip, messages: List[ChatMessage]) -> None:
        # Map wall-clock time to a position in the recording. --offset moves
        # the window earlier when chat reacts later than the recording shows.
        rec_start = clip.wall_start - self.recorder.started_at - self.args.offset
        wall_start = clip.wall_start
        if rec_start < 0:
            wall_start -= rec_start
            rec_start = 0.0
        duration = clip.wall_end - wall_start
        if duration < 3:
            log(f"skip spike at {time.strftime('%H:%M:%S', time.localtime(clip.spike_time))}: too little new footage")
            return

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clip.spike_time))
        out = self.args.clips_dir / f"{self.args.channel}_{stamp}_{clip.peak}msgs.mp4"
        if not cut_clip(self.recorder.path, rec_start, duration, out):
            log(f"ffmpeg failed for {out.name}")
            return
        if self.args.srt:
            out.with_suffix(".srt").write_text(build_chat_srt(messages, wall_start, clip.wall_end), encoding="utf-8")
        log(f"clip saved: {out} ({duration:.0f} s)")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="watch.sh", description="Auto-clip a live Twitch stream when chat spikes.")
    p.add_argument("channel", type=str.lower, help="Twitch channel name, e.g. xqc")
    p.add_argument("--min", dest="min_messages", type=int, default=10, help="minimum messages in a bucket to count as a spike (default 10)")
    p.add_argument("--factor", type=float, default=2.5, help="spike = this many times the recent median (default 2.5)")
    p.add_argument("--bucket", type=float, default=10, help="bucket size in seconds (default 10)")
    p.add_argument("--window", type=float, default=180, help="seconds of history for the median (default 180)")
    p.add_argument("--before", type=float, default=20, help="seconds of video before the spike (default 20)")
    p.add_argument("--after", type=float, default=10, help="seconds of video after the spike (default 10)")
    p.add_argument("--max-len", type=float, default=60, help="cap for clips merged from back-to-back spikes (default 60)")
    p.add_argument("--offset", type=float, default=0, help="shift clips this many seconds earlier to match chat delay (default 0)")
    p.add_argument("--quality", default="best", help="streamlink quality (default best)")
    p.add_argument("--rec-dir", type=Path, default=ROOT / "rec")
    p.add_argument("--clips-dir", type=Path, default=ROOT / "clips")
    p.add_argument("--no-srt", dest="srt", action="store_false", help="don't write chat .srt files")
    p.add_argument("-v", "--verbose", action="store_true", help="print every bucket's message count")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    window_buckets = max(1, int(args.window / args.bucket))
    detector = SpikeDetector(window_buckets, args.factor, args.min_messages, warmup_buckets=min(6, window_buckets))
    # Wait one extra bucket past a clip's end, so a spike in the next bucket
    # can still extend the clip before it is cut.
    planner = ClipPlanner(args.before, args.after, args.max_len, wait=args.bucket + 5)
    history: Deque[ChatMessage] = collections.deque()
    keep_seconds = args.before + args.max_len + 120

    recorder = Recorder(args.channel, args.rec_dir, args.quality)
    try:
        recorder.start()
    except RuntimeError as err:
        log(f"error: {err}")
        return 1

    worker = ClipWorker(args, recorder)
    worker.start()
    chat = TwitchChat(args.channel)
    counter = BucketCounter(args.bucket, time.time())
    backoff = 1.0

    def dispatch(clips: List[Clip]) -> None:
        for clip in clips:
            worker.submit(clip, [m for m in history if clip.wall_start <= m.time < clip.wall_end])

    try:
        while recorder.running():
            if chat.sock is None:
                try:
                    chat.connect()
                    backoff = 1.0
                except OSError as err:
                    log(f"chat: connect failed ({err}), retrying in {backoff:.0f} s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
            try:
                received = chat.poll()
            except OSError as err:
                log(f"chat: {err}, reconnecting")
                chat.close()
                continue

            now = time.time()
            for start, count in counter.tick(now):
                if detector.add(count):
                    log(f"SPIKE: {count} msgs (median {detector.baseline:g})")
                    planner.on_spike(start, count)
                elif args.verbose:
                    log(f"bucket: {count} msgs (median {detector.baseline:g})")
            counter.add(len(received))
            history.extend(ChatMessage(now, user, text) for user, text in received)
            while history and history[0].time < now - keep_seconds:
                history.popleft()
            dispatch(planner.due(now))
        log("stream ended")
    except KeyboardInterrupt:
        log("stopping")
    finally:
        chat.close()
        recorder.stop()
        dispatch(planner.drain())  # recording is final, cut whatever is left
        worker.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
