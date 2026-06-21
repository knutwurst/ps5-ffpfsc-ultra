#!/usr/bin/env bash
#
# rename_long_ffpfsc.sh
#
# One-off renamer for the .ffpfsc files whose names exceed ShadowMount's ~63-byte
# filename limit because the GAME TITLE is long (edition fluff). Each entry below maps a
# Title ID (unique, ASCII — robust to match on) to a hand-shortened target name.
#
# EDIT the MAP entries below to taste before applying — change any name you don't like.
# Files are matched by "*<TITLEID>*.ffpfsc" under the search dir and renamed IN PLACE
# (same folder). The folder names are left alone (only the path-component .ffpfsc matters
# to ShadowMount).
#
# SAFE BY DEFAULT: DRY RUN. Re-run with --apply (or -a) to actually rename.
#
# Usage:
#   ./rename_long_ffpfsc.sh "/Volumes/PS5 Games 1/PS5 Games"
#   ./rename_long_ffpfsc.sh "/Volumes/PS5 Games 1/PS5 Games" --apply
#
set -euo pipefail

# --- EDIT ME: "TITLEID|New base name (without .ffpfsc extension)" -------------------
MAP=(
  "PPSA21734|STALKER Shadow of Chornobyl [PPSA21734] [01.009]"
  "PPSA21732|STALKER Call of Pripyat [PPSA21732] [01.009]"
  "PPSA18152|Legacy of Kain Soul Reaver 1&2 [PPSA18152] [01.000]"
  "PPSA08705|Beyond Good & Evil [PPSA08705] [01.004]"
  "PPSA03977|The Witcher 3 Wild Hunt [PPSA03977] [04.040]"
  "PPSA16384|Directive 8020 [PPSA16384] [01.000]"
  "PPSA07809|Crisis Core FF7 Reunion [PPSA07809] [01.004]"
  "PPSA28329|RoboCop Rogue City Unfinished [PPSA28329] [01.005]"
)
# ------------------------------------------------------------------------------------

APPLY=0
ROOT=""
for arg in "$@"; do
  case "$arg" in
    --apply|-a) APPLY=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,22p'; exit 0 ;;
    *) ROOT="$arg" ;;
  esac
done
if [[ -z "$ROOT" ]]; then ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
if [[ ! -d "$ROOT" ]]; then echo "ERROR: not a directory: $ROOT" >&2; exit 1; fi

if [[ "$APPLY" -eq 1 ]]; then echo ">>> APPLY mode — files WILL be renamed."
else echo ">>> DRY RUN — nothing will be changed. Re-run with --apply to rename."; fi
echo ">>> Searching under: $ROOT"
echo

done_n=0; miss_n=0; skip_n=0
for entry in "${MAP[@]}"; do
  tid="${entry%%|*}"
  newbase="${entry#*|}"
  # First .ffpfsc whose name contains the Title ID.
  f="$(find "$ROOT" -type f -iname "*${tid}*.ffpfsc" -print 2>/dev/null | head -1)"
  if [[ -z "$f" ]]; then
    echo "  [MISS] no .ffpfsc found for $tid"
    miss_n=$((miss_n + 1))
    continue
  fi
  dir="$(dirname "$f")"
  cur="$(basename "$f")"
  new="${newbase}.ffpfsc"
  newbytes=$(printf '%s' "$new" | wc -c | tr -d ' ')
  if [[ "$cur" == "$new" ]]; then
    echo "  [OK]   already named: $cur"
    skip_n=$((skip_n + 1))
    continue
  fi
  if [[ -e "$dir/$new" ]]; then
    echo "  [SKIP] target exists: $new"
    skip_n=$((skip_n + 1))
    continue
  fi
  echo "  $cur"
  echo "    -> $new   (${newbytes} bytes)"
  if [[ "$APPLY" -eq 1 ]]; then mv -n -- "$dir/$cur" "$dir/$new"; fi
  done_n=$((done_n + 1))
done

echo
echo ">>> ${done_n} to rename, ${skip_n} already-ok/skipped, ${miss_n} not found."
if [[ "$APPLY" -eq 0 && "$done_n" -gt 0 ]]; then
  echo ">>> Re-run with --apply to perform the ${done_n} rename(s)."
fi
