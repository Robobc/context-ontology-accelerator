#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Fork drift report: how far has upstream moved since the base of the current
# patch series, and how hard will the next rebase be?
#
# Prints: upstream commits and tags since our base, files changed on both sides,
# the dry-run conflict list (git merge-tree, no checkout), and per-patch overlap
# so the costly patches are visible before the rebase starts.
#
# Usage:
#   scripts/fork-drift-report.sh [SERIES_REF] [UPSTREAM_REF]
#     SERIES_REF    tip of the patch series (default: HEAD)
#     UPSTREAM_REF  upstream branch to compare against (default: origin/main)
#   DRIFT_NO_FETCH=1 skips the fetch of the upstream remote (offline use).
# Exit status: 0 when the dry-run merge is clean, 1 when it has conflicts,
# so the script doubles as a check in a weekly job.
set -euo pipefail

SERIES="${1:-HEAD}"
UPSTREAM="${2:-origin/main}"

# A report against stale local refs says "0 commits since base" while upstream
# has tagged a release (observed 2026-10-25: v0.3.7 missed for three days), so
# refresh the upstream remote first unless explicitly told not to.
if [ "${DRIFT_NO_FETCH:-0}" != "1" ] && [[ "$UPSTREAM" == */* ]]; then
  git fetch --quiet --tags "${UPSTREAM%%/*}" || echo "warning: fetch of ${UPSTREAM%%/*} failed; reporting against local refs" >&2
fi

git rev-parse --verify --quiet "$UPSTREAM" >/dev/null || {
  echo "upstream ref '$UPSTREAM' not found; run: git fetch origin --tags" >&2
  exit 2
}

BASE="$(git merge-base "$UPSTREAM" "$SERIES")"
UP_HEAD="$(git rev-parse --short "$UPSTREAM")"

echo "# Fork drift report ($(date -u +%Y-%m-%dT%H:%MZ))"
echo
echo "series: $(git rev-parse --short "$SERIES") ($SERIES)"
echo "base:   $(git rev-parse --short "$BASE") $(git tag --points-at "$BASE" | tr '\n' ' ')"
echo "upstream head: $UP_HEAD ($UPSTREAM)"
echo

echo "## Upstream since base"
echo "commits: $(git rev-list --count "$BASE".."$UPSTREAM")"
echo "tags:    $(git tag --merged "$UPSTREAM" --no-merged "$BASE" | sort -V | tr '\n' ' ')"
git diff --shortstat "$BASE" "$UPSTREAM" | sed 's/^/delta:   /'
echo

echo "## Patch series since base"
PATCHES="$(git rev-list --reverse "$BASE".."$SERIES")"
echo "patches: $(printf '%s\n' "$PATCHES" | grep -c . || true)"
echo

UP_FILES="$(git diff --name-only "$BASE" "$UPSTREAM" | sort)"
OURS_FILES="$(git diff --name-only "$BASE" "$SERIES" | sort)"
BOTH="$(comm -12 <(printf '%s\n' "$UP_FILES") <(printf '%s\n' "$OURS_FILES"))"
echo "## Files changed on both sides: $(printf '%s\n' "$BOTH" | grep -c . || true)"
printf '%s\n' "$BOTH" | sed 's/^/  /'
echo

echo "## Dry-run merge (git merge-tree, no checkout)"
CONFLICT_RC=0
MT_OUT="$(git merge-tree --write-tree --name-only "$UPSTREAM" "$SERIES" 2>/dev/null)" || CONFLICT_RC=$?
if [ "$CONFLICT_RC" -eq 0 ]; then
  echo "clean: no conflicts"
else
  # line 1 is the tree OID, then conflicted paths, then a blank line and
  # informational messages we do not want
  CONFLICTS="$(printf '%s\n' "$MT_OUT" | tail -n +2 | awk 'NF==0{exit} {print}')"
  echo "conflicts: $(printf '%s\n' "$CONFLICTS" | grep -c . || true)"
  printf '%s\n' "$CONFLICTS" | sed 's/^/  /'
fi
echo

echo "## Per-patch exposure (files touched / of which upstream also changed)"
printf '%-9s %5s %7s  %s\n' commit files overlap subject
for c in $PATCHES; do
  FILES="$(git show --format= --name-only "$c" | sort)"
  N="$(printf '%s\n' "$FILES" | grep -c . || true)"
  O="$(comm -12 <(printf '%s\n' "$FILES") <(printf '%s\n' "$UP_FILES") | grep -c . || true)"
  printf '%-9s %5s %7s  %s\n' "$(git rev-parse --short "$c")" "$N" "$O" "$(git log -1 --format=%s "$c")"
done

exit "$CONFLICT_RC"
