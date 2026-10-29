#!/usr/bin/env bash
# Stop hook: print slice progress from the status table in docs/PLAN.md after every response.
PLAN="$CLAUDE_PROJECT_DIR/docs/PLAN.md"
[ -f "$PLAN" ] || exit 0

# Table rows look like: | 3 | Walking skeleton: ask → table, streamed | todo | notes |
ROWS=$(grep -E '^\| *[0-9]+ *\|' "$PLAN")
TOTAL=$(printf '%s\n' "$ROWS" | grep -c .)
DONE=$(printf '%s\n' "$ROWS" | awk -F'|' 'tolower($4) ~ /^ *done *$/' | grep -c .)
NEXT=$(printf '%s\n' "$ROWS" | awk -F'|' 'tolower($4) !~ /^ *done *$/ {
  gsub(/^ +| +$/, "", $2); gsub(/^ +| +$/, "", $3); print "Slice " $2 " - " $3; exit }')

if [ "$TOTAL" -eq 0 ]; then
  exit 0
elif [ "$DONE" -eq "$TOTAL" ]; then
  MSG="PLAN.md: all $TOTAL slices done."
else
  MSG="PLAN.md: $DONE of $TOTAL slices completed | next: $NEXT"
fi
jq -n --arg msg "$MSG" '{systemMessage: $msg}'
exit 0
