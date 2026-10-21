#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Identifier scan: refuse to tag a release whose patch series carries deployment
# or customer identifiers.
#
# Two pattern sets, two scopes:
#   * built in (safe to publish): AWS account ids, ARNs, e-mail addresses and
#     GUIDs. Upstream's own fixtures are full of placeholder values, so these
#     run only on the lines the series ADDS relative to the upstream base, with
#     a small allowlist of documented placeholders (scripts/identifier-scan.allow).
#   * private (never committed): one extended regex per line in the file named
#     by IDENTIFIER_SCAN_PATTERNS. Customer names, tenant ids, catalogue and
#     column names belong there, not in this script. These are specific enough
#     to run over the whole tracked tree and every commit message in the series.
#
# Usage:
#   scripts/identifier-scan.sh [BASE_REF]
#     BASE_REF  upstream base of the series (default: merge-base of origin/main and HEAD)
# Env:
#   IDENTIFIER_SCAN_PATTERNS   file of private extended regexes (optional)
#   IDENTIFIER_SCAN_ALLOW      allowlist file (default: scripts/identifier-scan.allow)
# Exit status: 0 clean, 1 findings, 2 usage error.
set -uo pipefail

ALLOW_FILE="${IDENTIFIER_SCAN_ALLOW:-scripts/identifier-scan.allow}"
PRIVATE_FILE="${IDENTIFIER_SCAN_PATTERNS:-}"
BASE_REF="${1:-$(git merge-base origin/main HEAD 2>/dev/null)}"
[ -n "$BASE_REF" ] || { echo "cannot determine base; pass BASE_REF or fetch origin" >&2; exit 2; }

BUILTIN=(
  'arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:[0-9]{12}:'
  '[Aa]ccount[^0-9]{0,20}[0-9]{12}([^0-9]|$)'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
  '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
)

PRIVATE=()
if [ -n "$PRIVATE_FILE" ]; then
  [ -r "$PRIVATE_FILE" ] || { echo "IDENTIFIER_SCAN_PATTERNS='$PRIVATE_FILE' not readable" >&2; exit 2; }
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    PRIVATE+=("$line")
  done < "$PRIVATE_FILE"
fi

allow_filter() {
  if [ -r "$ALLOW_FILE" ]; then grep -vE -f "$ALLOW_FILE"; else cat; fi
}

FINDINGS=0
report() { # report <label> <matches>
  [ -z "$2" ] && return 0
  echo "## $1"; printf '%s\n' "$2"; echo
  FINDINGS=$((FINDINGS + $(printf '%s\n' "$2" | grep -c .)))
}

# 1. Generic patterns on added lines only, reported as "path: line".
ADDED="$(git diff "$BASE_REF" HEAD -- . ":(exclude)$ALLOW_FILE" \
  | awk '/^\+\+\+ b\//{f=substr($0,7)} /^\+[^+]/{print f ": " substr($0,2)}')"
for p in "${BUILTIN[@]}"; do
  OUT="$(printf '%s\n' "$ADDED" | grep -E -e "$p" | allow_filter)" || true
  report "added since $BASE_REF: $p" "$OUT"
done

# 2. Private patterns on the whole tracked tree and on the series' messages.
if [ "${#PRIVATE[@]}" -gt 0 ]; then
  FILE_LIST="$(git ls-files -z \
    | grep -zvE '\.(png|jpg|jpeg|gif|pdf|ico|woff2?|ttf|jar|lock|lockb)$|pnpm-lock\.yaml$|uv\.lock$' \
    | tr '\0' '\n')"
  MSGS="$(git log --format='%h %B' "$BASE_REF"..HEAD)"
  for p in "${PRIVATE[@]}"; do
    OUT="$(printf '%s\n' "$FILE_LIST" | tr '\n' '\0' | xargs -0 grep -nIE -e "$p" -- 2>/dev/null)" || true
    report "tracked files: $p" "$OUT"
    OUT="$(printf '%s\n' "$MSGS" | grep -nE -e "$p")" || true
    report "commit messages $BASE_REF..HEAD: $p" "$OUT"
  done
fi

if [ "$FINDINGS" -gt 0 ]; then
  echo "identifier scan: $FINDINGS finding(s)"; exit 1
fi
echo "identifier scan: clean (base $(git rev-parse --short "$BASE_REF"), ${#BUILTIN[@]} generic + ${#PRIVATE[@]} private patterns)"
