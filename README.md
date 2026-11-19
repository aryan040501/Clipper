# clip-watcher

Free, automatic Twitch clipping. No AI, no API keys, no credits.

```
./watch.sh <twitch_channel>
```

1. **Record**: `streamlink` records the live stream to `rec/<channel>_<time>.ts`.
2. **Read chat**: Twitch chat is read anonymously over IRC (`irc.chat.twitch.tv:6667`, nick `justinfan12345`, no token).
3. **Find spikes**: messages are counted in 10 s buckets. A bucket is a spike when it has at least 2.5x the median of the last 3 minutes **and** at least `--min` messages.
4. **Clip**: for each spike, `ffmpeg` cuts 20 s before to 10 s after into `clips/`, center-cropped to 9:16. Each clip gets a matching `.srt` with the chat from that moment.

## Install

```
brew install ffmpeg          # or apt install ffmpeg
pip install streamlink       # or: pip install -r requirements.txt
```

Python 3.9+ is needed. Nothing else is required, because the rest is the standard library.

## Usage

```
./watch.sh xqc                 # defaults
./watch.sh xqc --min 25        # big channel: need at least 25 msgs / 10 s
./watch.sh smallstreamer --min 5 --factor 3
./watch.sh xqc --offset 3      # chat reacts ~3 s late, so shift clips earlier
./watch.sh --help              # all options
```

Stop with Ctrl+C. Spikes that are still waiting are cut from what was recorded before the program exits.

| Option | Default | Meaning |
| --- | --- | --- |
| `--min` | 10 | minimum messages in a bucket to count as a spike |
| `--factor` | 2.5 | spike = this many times the recent median |
| `--bucket` | 10 | bucket size, seconds |
| `--window` | 180 | history used for the median, seconds |
| `--before` / `--after` | 20 / 10 | seconds of video around the spike |
| `--max-len` | 60 | cap for clips merged from back-to-back spikes |
| `--offset` | 0 | shift clips earlier by this many seconds |
| `--quality` | best | streamlink quality (`720p60`, `480p`, ...) |
| `--no-srt` | | don't write chat subtitles |
| `-v` | | print every bucket's count |

### How it behaves

- **Warm-up:** no spikes are reported in the first minute, while the median builds up.
- **Back-to-back spikes merge:** if chat stays hot, the next spike extends the clip that is still waiting (up to `--max-len`) instead of making overlapping clips. Each clip is cut about 15 s after its window ends, so a follow-up spike can still extend it.
- **Timing:** a recording position is matched to a wall-clock time from when the recording started. Twitch ads are recorded rather than skipped, so this mapping does not drift. If clips start too late, raise `--offset`.
- **Disk:** the full stream is kept in `rec/`. Delete it when you're done.
- Clip names show the spike's peak: `clips/xqc_20261119-201502_84msgs.mp4`.

## Subtitles (.srt) without AI

There are a few ways to get subtitles without paying for AI:

1. **Chat as subtitles (built in, on by default).** Each clip gets `clips/<name>.srt` with the chat messages sent during the clip, timed to when they arrived. It shows up to 3 recent lines at a time. This needs no model at all, and chat reactions are often what makes a clip worth watching.
2. **The streamer's own closed captions.** Some streamers broadcast CEA-608 captions. When they do, ffmpeg can extract them from the recording without any speech recognition:
   ```
   ffmpeg -f lavfi -i "movie=rec/<file>.ts[out0+subcc]" -map 0:s captions.srt
   ```
   Few streams carry them, so this isn't automated.
3. **Speech-to-text on your own machine.** If you want subtitles of what the streamer *says*, the only way is speech recognition. [whisper.cpp](https://github.com/ggerganov/whisper.cpp) or [Vosk](https://alphacephei.com/vosk/) run fully offline, for free, with no account or credits. They are still ML models, which is why they aren't built in. Example with whisper.cpp:
   ```
   ffmpeg -i clips/x.mp4 -ar 16000 -ac 1 /tmp/x.wav
   whisper-cli -m models/ggml-base.en.bin -f /tmp/x.wav -osrt -of clips/x
   ```

To burn an .srt into the video (ffmpeg needs to be built with libass, which Homebrew's build is):

```
ffmpeg -i clips/x.mp4 -vf "subtitles=clips/x.srt:force_style='FontSize=14,Alignment=2'" -c:a copy clips/x_subbed.mp4
```

## Tests

```
python3 -m unittest
```
