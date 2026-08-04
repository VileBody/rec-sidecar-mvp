#!/bin/sh
set -eu

desktop_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(CDPATH= cd -- "$desktop_dir/.." && pwd)
dist_dir="$desktop_dir/dist"

mkdir -p "$dist_dir"
cp "$desktop_dir/ui/index.html" "$dist_dir/index.html"
cp "$desktop_dir/ui/app.js" "$dist_dir/app.js"
cp "$desktop_dir/ui/pip.html" "$dist_dir/pip.html"
cp "$desktop_dir/ui/pip.js" "$dist_dir/pip.js"
cp "$desktop_dir/ui/desktop.css" "$dist_dir/desktop.css"
cp "$repo_dir/clean_start/internal/clean/web/styles.css" "$dist_dir/styles.css"

echo "Prepared REC Coach desktop UI in $dist_dir"
