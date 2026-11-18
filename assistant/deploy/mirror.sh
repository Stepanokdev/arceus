#!/bin/bash
set -euo pipefail
SRC=${1:?workspace dir}; DST=${2:?mirror dir}
mkdir -p "$DST"
for repo in "$SRC"/*/ "$SRC"/*/*/; do
  [ -d "$repo/.git" ] || continue
  name=${repo#"$SRC"/}; name=${name%/}
  mkdir -p "$DST/$(dirname "$name")"
  url=$(git -C "$repo" remote get-url origin 2>/dev/null) || continue
  if [ ! -d "$DST/$name/.git" ]; then
    git clone --quiet --reference-if-able "$repo" "$url" "$DST/$name" || { echo "skip $name: clone failed" >&2; continue; }
  fi
  branch=$(git -C "$DST/$name" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
  branch=${branch:-$(git -C "$repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')}
  branch=${branch:-master}
  git -C "$DST/$name" fetch --quiet origin "$branch" || { echo "skip $name: fetch failed" >&2; continue; }
  git -C "$DST/$name" checkout --quiet -B "$branch" "origin/$branch"
  git -C "$DST/$name" reset --quiet --hard "origin/$branch"
  git -C "$DST/$name" clean --quiet -fdx
done
