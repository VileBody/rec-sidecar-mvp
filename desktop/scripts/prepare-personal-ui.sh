#!/bin/sh
set -eu

desktop_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(CDPATH= cd -- "$desktop_dir/.." && pwd)
dist_dir="$desktop_dir/personal-dist"

mkdir -p "$dist_dir"
cp "$desktop_dir/personal-ui/index.html" "$dist_dir/index.html"
cp "$desktop_dir/personal-ui/app.js" "$dist_dir/app.js"
cp "$desktop_dir/personal-ui/personal.css" "$dist_dir/personal.css"
cp "$repo_dir/clean_start/internal/clean/web/styles.css" "$dist_dir/styles.css"

echo "Prepared REC Personal desktop UI in $dist_dir"
