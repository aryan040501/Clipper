#!/usr/bin/env bash
# clip-watcher: ./watch.sh <twitch_channel> [--min N] [--factor X] [...]
# Run ./watch.sh --help for every option.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: ./watch.sh <twitch_channel> [options]   (./watch.sh --help for options)" >&2
  exit 1
fi

for bin in python3 streamlink ffmpeg; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "missing dependency: $bin  (install: brew install ffmpeg && pip install streamlink)" >&2
    exit 1
  fi
done

exec python3 "$(dirname "$0")/clip_watcher.py" "$@"
