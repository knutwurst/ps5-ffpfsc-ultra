#!/usr/bin/env bash
#
# check_ffpfsc_name_lengths.sh
#
# Lists .ffpfsc / .ffpfs files whose FILENAME is too long for ShadowMountPlus.
# SMP rejects over-long names with ENAMETOOLONG ("Dateiname zu lang"), which counts
# UTF-8 BYTES, not characters (a "™" is 3 bytes). Observed: 59 bytes mounts, 69 fails, so
# the cap is ~64; this script counts BYTES and defaults MAX to 63 (conservative). Override
# by passing a different MAX.
#
# Searches the given directory recursively. If no directory is passed, it uses
# the directory this script lives in.
#
# Read-only: never renames or deletes anything.
#
# Usage:
#   ./check_ffpfsc_name_lengths.sh                  # script's own dir, limit 255
#   ./check_ffpfsc_name_lengths.sh /Volumes/Games   # given dir
#   ./check_ffpfsc_name_lengths.sh /Volumes/Games 240   # custom limit
#
set -euo pipefail

MAX=63
ROOT=""
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,20p'
      exit 0 ;;
    *[!0-9]*) ROOT="$arg" ;;     # contains a non-digit -> treat as path
    *) MAX="$arg" ;;             # all digits -> custom limit
  esac
done

if [[ -z "$ROOT" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: not a directory: $ROOT" >&2
  exit 1
fi

# "near limit" warning band: within 15 of the cap.
NEAR=$((MAX - 15))

echo ">>> Limit: $MAX bytes per filename (ShadowMountPlus ENAMETOOLONG; bytes, not chars)."
echo ">>> Searching under: $ROOT"
echo

# Build "length<TAB>fullpath" lines, counting the BASENAME in UTF-8 BYTES (wc -c),
# because ENAMETOOLONG is a byte limit — a "™" costs 3 bytes, not 1.

total=0
over=0
near=0
longest=0
longest_name=""

# Collect over-limit and near-limit names into temp arrays via a temp file
# (keeps it sorted, longest first).
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

while IFS= read -r -d '' f; do
  total=$((total + 1))
  base="$(basename "$f")"
  len=$(printf '%s' "$base" | wc -c | tr -d ' ')   # UTF-8 byte length
  if (( len > longest )); then longest=$len; longest_name="$base"; fi
  if (( len > MAX )); then
    printf '%05d\tOVER\t%s\n' "$len" "$f" >> "$tmp"
    over=$((over + 1))
  elif (( len >= NEAR )); then
    printf '%05d\tNEAR\t%s\n' "$len" "$f" >> "$tmp"
    near=$((near + 1))
  fi
done < <(find "$ROOT" -type f \( -iname '*.ffpfsc' -o -iname '*.ffpfs' \) -print0)

if (( over > 0 )); then
  echo "=== TOO LONG (> $MAX) ==="
  sort -rn "$tmp" | awk -F'\t' '$2=="OVER"{ printf "  %3d bytes  %s\n", $1+0, $3 }'
  echo
fi
if (( near > 0 )); then
  echo "=== CLOSE TO THE LIMIT ($NEAR-$MAX) ==="
  sort -rn "$tmp" | awk -F'\t' '$2=="NEAR"{ printf "  %3d bytes  %s\n", $1+0, $3 }'
  echo
fi

echo ">>> $total file(s) scanned."
if (( total == 0 )); then
  echo ">>> WARNING: no .ffpfsc/.ffpfs files were read — empty folder, wrong path, or a"
  echo ">>>          permission-protected drive. On macOS, run this from a terminal with"
  echo ">>>          Full Disk Access (System Settings → Privacy & Security)."
  exit 0
fi
echo ">>> $over over the limit, $near close to it."
echo ">>> Longest name: $longest bytes — $longest_name"
if (( over == 0 )); then
  echo ">>> OK — no filename exceeds $MAX bytes."
fi
