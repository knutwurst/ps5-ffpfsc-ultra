#!/usr/bin/env bash
#
# shorten_ffpfsc_versions.sh
#
# Shortens the version tag in .ffpfsc / .ffpfs filenames AND strips decorative symbols
# (™ ® © ℠ ℗) that waste bytes against ShadowMount's filename limit:
#     v01.123.456  ->  01.123        (drop the leading "v", drop the 3rd group)
#     [v01.007.000] -> [01.007]      (brackets are preserved)
#     [vv01.027.000] -> [01.027]     (handles stray double-"v" too)
#     The Last of Us™ ... -> The Last of Us ...   (™/®/© removed)
#
# Searches the given directory recursively. If no directory is passed, it uses
# the directory this script lives in.
#
# SAFE BY DEFAULT: this is a DRY RUN — it only prints what it WOULD rename.
# Re-run with --apply (or -a) to actually rename the files.
#
# Usage:
#   ./shorten_ffpfsc_versions.sh                 # dry run, script's own dir
#   ./shorten_ffpfsc_versions.sh /Volumes/Games  # dry run, given dir
#   ./shorten_ffpfsc_versions.sh /Volumes/Games --apply
#
set -euo pipefail

APPLY=0
ROOT=""
for arg in "$@"; do
  case "$arg" in
    --apply|-a) APPLY=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,30p'
      exit 0 ;;
    *) ROOT="$arg" ;;
  esac
done

# Default root = the directory this script resides in.
if [[ -z "$ROOT" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: not a directory: $ROOT" >&2
  exit 1
fi

if [[ "$APPLY" -eq 1 ]]; then
  echo ">>> APPLY mode — files WILL be renamed."
else
  echo ">>> DRY RUN — nothing will be changed. Re-run with --apply to rename."
fi
echo ">>> Searching under: $ROOT"
echo

# Compute the shortened version of a *basename*.
# Three ordered passes (perl PCRE):
#   1) bracketed:   [v*NN.NNN(.NNN)?]  -> [NN.NNN]
#   2) v-prefixed:  v+NN.NNN(.NNN)?    -> NN.NNN
#   3) bare 3-group: NN.NNN.NNN        -> NN.NNN   (collapse trailing group)
# Then tidy any double spaces / trailing space before the extension.
shorten_name() {
  perl -CS -pe '
    s/[\x{2122}\x{00ae}\x{00a9}\x{2120}\x{2117}]//g;
    s/\[v*(\d{1,2}\.\d{2,3})(?:\.\d{1,3})?\]/[$1]/g;
    s/\bv+(\d{1,2}\.\d{2,3})(?:\.\d{1,3})?\b/$1/g;
    s/\b(\d{1,2}\.\d{2,3})\.\d{1,3}\b/$1/g;
    s/  +/ /g;
    s/ +(\.[A-Za-z0-9]+)$/$1/;
    s/ +\]/]/g;
    s/\[ +/[/g;
  '
}

count_total=0
count_change=0
count_skip=0

# -print0 / read -d '' for names with spaces.
while IFS= read -r -d '' f; do
  count_total=$((count_total + 1))
  dir="$(dirname "$f")"
  base="$(basename "$f")"
  new="$(printf '%s' "$base" | shorten_name)"

  if [[ "$new" == "$base" ]]; then
    continue
  fi

  if [[ -e "$dir/$new" ]]; then
    echo "  [SKIP] target exists: $base  ->  $new"
    count_skip=$((count_skip + 1))
    continue
  fi

  count_change=$((count_change + 1))
  echo "  $base"
  echo "    -> $new"
  if [[ "$APPLY" -eq 1 ]]; then
    mv -n -- "$dir/$base" "$dir/$new"
  fi
done < <(find "$ROOT" -type f \( -iname '*.ffpfsc' -o -iname '*.ffpfs' \) -print0)

echo
if [[ "$count_total" -eq 0 ]]; then
  echo ">>> WARNING: no .ffpfsc/.ffpfs files were read — empty folder, wrong path, or a"
  echo ">>>          permission-protected drive. On macOS, run this from a terminal with"
  echo ">>>          Full Disk Access (System Settings → Privacy & Security)."
  exit 0
fi
echo ">>> $count_total file(s) scanned, $count_change to rename, $count_skip skipped (target existed)."
if [[ "$APPLY" -eq 0 && "$count_change" -gt 0 ]]; then
  echo ">>> Re-run with --apply to perform the $count_change rename(s)."
fi
