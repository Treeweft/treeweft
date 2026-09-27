#!/usr/bin/env bash
# Prints, as Markdown, the packages `uv lock --upgrade` changed that are
# installed in the current environment. Used by the "Upgraded dependencies"
# workflow (.github/workflows/upgraded-dependencies.yml).
#
# Reads changes.log: the "Updated|Added|Removed <package> ..." lines of
# `uv lock --upgrade`. Set FREEZE_CMD to replace `uv pip freeze` (tests).
set -uo pipefail

changes="${1:-changes.log}"
echo "### Upgraded packages (python ${PYTHON:-?})"
echo

if [[ ! -s "$changes" ]]; then
  echo "None: uv.lock already has the newest versions in range, or the upgrade did not run."
  exit 0
fi

# Package names compare case-insensitively, with runs of - _ . equal (PEP 503).
normalize() { tr '[:upper:]' '[:lower:]' | sed -E 's/[-_.]+/-/g'; }

installed=$(${FREEZE_CMD:-uv pip freeze} 2>/dev/null | sed -E 's/[ =@<>~!;].*//' | normalize | sort -u)
total=$(wc -l < "$changes" | tr -d ' ')

if [[ -z "$installed" ]]; then
  echo "The environment could not be read, so every package that differs from uv.lock is listed ($total)."
  echo
  echo '```'
  cat "$changes"
  echo '```'
  exit 0
fi

shown=$(while read -r line; do
  name=$(awk '{print $2}' <<< "$line" | normalize)
  grep -qxF "$name" <<< "$installed" && echo "$line"
done < "$changes")
count=$(grep -c . <<< "$shown" || true)

echo "$count of the $total packages that differ from uv.lock are installed by this job:"
echo
echo '```'
echo "$shown"
echo '```'
