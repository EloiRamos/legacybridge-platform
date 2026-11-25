#!/usr/bin/env bash
# Claims audit (V9-1 / DOC-01). Greps README.md and docs/ for claim patterns that the project must not
# make (exactly-once, idempotency, production readiness, ROI, productivity, real customers, deployed). Every hit must be
# allowlisted in docs/portfolio/claims-allowlist.txt as "<file>|<substring of the line>" — allowlisted lines are
# negations or disclaimers ("not claimed", "no idempotency", …). Exit 0: no unallowlisted hit.
#   --labels  (DOC-02) also require the fictional/simulated disclaimer in README, CASE_STUDY, engagement documents and
#             the console footer.
set -uo pipefail
cd "$(dirname "$0")/.."
ALLOW=docs/portfolio/claims-allowlist.txt
PATTERN='exactly[- ]once|idempoten|production[- ]ready|\bROI\b|productivity|real customer|deployed'
fail=0
while IFS= read -r hit; do
  file="${hit%%:*}"; rest="${hit#*:}"; line="${rest#*:}"
  allowed=0
  while IFS='|' read -r afile asub; do
    [ -z "$afile" ] || [ "${afile:0:1}" = "#" ] && continue
    if [ "$afile" = "$file" ] && [[ "$line" == *"$asub"* ]]; then allowed=1; break; fi
  done < "$ALLOW"
  if [ $allowed = 0 ]; then echo "UNALLOWLISTED $hit"; fail=1; fi
done < <(grep -rniE "$PATTERN" README.md docs --include='*.md' --include='*.txt' | grep -v "^$ALLOW:")
if [ "${1:-}" = "--labels" ]; then
  for f in README.md docs/portfolio/CASE_STUDY.md docs/engagement/*.md; do
    grep -qiE "fictional" "$f" && grep -qiE "simulat" "$f" || { echo "MISSING-LABEL $f"; fail=1; }
  done
  grep -rq "Fictional engagement" frontend/src || { echo "MISSING-LABEL console footer"; fail=1; }
fi
[ $fail = 0 ] && echo "claims audit: clean"
exit $fail
