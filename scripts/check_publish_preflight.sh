#!/usr/bin/env bash
# Pre-publish checks for a public GitHub / hosted demo cut.
# Prints path and rule names only — never secret values or ledger contents.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

failures=0

echo "== publish preflight =="

if [[ -x ./scripts/check_public_demo_secrets.sh ]]; then
  if ./scripts/check_public_demo_secrets.sh; then
    echo "OK: public demo secrets hygiene"
  else
    echo "FAIL: public demo secrets hygiene"
    failures=$((failures + 1))
  fi
else
  echo "FAIL: missing scripts/check_public_demo_secrets.sh"
  failures=$((failures + 1))
fi

require_ignore() {
  local file="$1"
  local pattern="$2"
  if grep -Fq "$pattern" "$file" 2>/dev/null; then
    echo "OK: ${file} excludes ${pattern}"
  else
    echo "FAIL: ${file} missing ${pattern}"
    failures=$((failures + 1))
  fi
}

for pattern in '.env' 'artifacts/' '*.db' '.venv/' 'data/'; do
  require_ignore .gitignore "$pattern"
done

# Tracked-tree must not contain private eval/budget paths or real env files.
# `.env.example` is an intentional public template.
while IFS= read -r path; do
  case "$path" in
    .env.example)
      ;;
    artifacts/*|.env|.env.*|*.sqlite3|*deepseek-budget*)
      echo "FAIL: tracked sensitive path: ${path}"
      failures=$((failures + 1))
      ;;
  esac
done < <(git ls-files)

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if git status --porcelain | grep -E '(^.. |\.env$|artifacts/)' >/dev/null 2>&1; then
    # Soft warning: dirty tree is OK for local work, but block if .env or artifacts
    # appear as untracked/staged candidates that look publishable.
    if git status --porcelain | grep -E '(^\?\? |\.env|artifacts/)' | grep -E '(\.env$|artifacts/)' >/dev/null 2>&1; then
      echo "FAIL: working tree shows .env or artifacts/ entries — keep them untracked and out of commits"
      failures=$((failures + 1))
    else
      echo "OK: no .env / artifacts/ publish candidates in status"
    fi
  else
    echo "OK: no .env / artifacts/ publish candidates in status"
  fi
fi

if [[ -f docs/12_phase6_publish_checklist.md ]]; then
  echo "OK: Phase 6 checklist present"
else
  echo "FAIL: missing docs/12_phase6_publish_checklist.md"
  failures=$((failures + 1))
fi

if grep -q 'holdout v2' README.md && grep -q '44/80' README.md; then
  echo "OK: README states holdout v2 FAIL aggregate"
else
  echo "FAIL: README missing honest holdout v2 FAIL aggregate"
  failures=$((failures + 1))
fi

if [[ "$failures" -ne 0 ]]; then
  echo "== result: FAIL (${failures}) =="
  exit 1
fi

echo "== result: PASS =="
echo "Next (needs explicit author auth): create public remote, push, optional host."
