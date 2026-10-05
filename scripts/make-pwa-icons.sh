#!/usr/bin/env bash
# Build the installed app's icons from the transparent mark (client/public/icon-512.png).
#
#   scripts/make-pwa-icons.sh
#
# iOS ignores transparency on a Home Screen icon — it paints the transparent
# parts black, or worse, white — and applies its own rounded-corner mask, so
# every icon here is an OPAQUE, full-bleed square: the mark composited onto
# Arc's page background (#080b11). The browser-tab favicons (icon-32/64) keep
# their alpha; a tab draws them on whatever the tab strip is.
#
# ffmpeg rather than Pillow: it is already on every machine that runs Arc (the
# worker needs it), so this adds no dependency to anything. Output is rgb24,
# i.e. a PNG with no alpha channel at all, which `sips -g hasAlpha` confirms.
set -euo pipefail

cd "$(dirname "$0")/../client/public"

SOURCE=icon-512.png
BACKGROUND=0x080b11

# render <out> <canvas px> <mark px>
render() {
  local out=$1 canvas=$2 mark=$3
  ffmpeg -loglevel error -y \
    -f lavfi -i "color=c=${BACKGROUND}:s=${canvas}x${canvas}" \
    -i "$SOURCE" \
    -filter_complex "[1:v]scale=${mark}:${mark}:flags=lanczos[m];[0:v][m]overlay=(W-w)/2:(H-h)/2:format=auto,format=rgb24" \
    -frames:v 1 -update 1 "$out"
  echo "$out ${canvas}x${canvas}"
}

# The mark already sits inside its own margin, so the plain icons use it at
# full size.
render pwa-512x512.png 512 512
render pwa-192x192.png 192 192
render apple-touch-icon-180x180.png 180 180

# Maskable: Android launchers may crop to a circle whose safe zone is the
# middle 80 %; the mark's glow reaches its edges, so it is drawn at 72 %.
render pwa-maskable-512x512.png 512 368
