#!/usr/bin/env bash
# Pack each finished day of raw data into one .tar.gz and delete the loose files.
# Today's folder (UTC) is left alone because the poller is still writing to it.
# Run nightly from cron (crontab -e):
#   30 0 * * * /home/ubuntu/arbcheck/deploy/compress_raw.sh
set -euo pipefail

cd "$(dirname "$0")/../data/raw"
today=$(date -u +%F)

for dir in */20??-??-??; do
    [ -d "$dir" ] || continue
    [ "$(basename "$dir")" = "$today" ] && continue
    if [ -e "$dir.tar.gz" ]; then
        echo "Skipping $dir: $dir.tar.gz already exists" >&2
        continue
    fi
    tar czf "$dir.tar.gz.tmp" -C "$(dirname "$dir")" "$(basename "$dir")"
    mv "$dir.tar.gz.tmp" "$dir.tar.gz"
    rm -rf "$dir"
done
