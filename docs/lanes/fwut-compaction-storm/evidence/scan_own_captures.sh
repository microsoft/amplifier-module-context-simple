#!/usr/bin/env bash
# Safe capture scan (v2, resumable). grep only -- no file parsed whole.
ROOT=/home/bkrabach/dev/openai-evals-team-ci/.amplifier/evaluation
OUT=/tmp/fwut/captures_scan2.tsv
DONEF=/tmp/fwut/scanned_files.txt
touch "$OUT" "$DONEF"
find "$ROOT" \( -name 'events.jsonl' -o -name 'root_events.jsonl' \) -print0 2>/dev/null |
while IFS= read -r -d '' f; do
  grep -qxF "$f" "$DONEF" && continue
  c=$(grep -c '"context:compaction"' "$f" 2>/dev/null || true)
  c=${c:-0}
  echo "$f" >> "$DONEF"
  [ "$c" -eq 0 ] 2>/dev/null && continue
  stats=$(grep -o '"message_count": *[0-9]\+' "$f" 2>/dev/null | grep -o '[0-9]\+$' |
    sort -n | awk '{a[n++]=$1} END{if(n==0){print "0\t0\t0"; exit} print a[0]"\t"a[int(n/2)]"\t"n}')
  printf '%s\t%s\t%s\n' "$c" "$stats" "$f" >> "$OUT"
done
echo "DONE files=$(wc -l < "$DONEF") with_compactions=$(wc -l < "$OUT")"
