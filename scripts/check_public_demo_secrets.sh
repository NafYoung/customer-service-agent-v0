#!/usr/bin/env bash
# Scan Docker *build context candidates* for secret-shaped paths/patterns.
# Prints only matched path/pattern names — never file contents or secret values.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

failures=0

check_absent() {
  local label="$1"
  shift
  local found=0
  local path
  for path in "$@"; do
    if [[ -e "$path" ]]; then
      echo "FAIL: present in tree (should stay out of image context): ${label}"
      found=1
      failures=$((failures + 1))
      break
    fi
  done
  if [[ "$found" -eq 0 ]]; then
    echo "OK: absent — ${label}"
  fi
}

echo "== public demo publish hygiene (paths only) =="

# Real env / DB / private artifacts must not be bakeable. .env.example is OK.
if [[ -f .env ]]; then
  if grep -q '^\.env$' .dockerignore 2>/dev/null; then
    echo "OK: .env exists locally but is listed in .dockerignore"
  else
    echo "FAIL: .env exists and is not excluded by .dockerignore"
    failures=$((failures + 1))
  fi
else
  echo "OK: no .env in tree"
fi

for pattern in '.venv' 'artifacts' '*.db' '*.sqlite' '*.sqlite3'; do
  if grep -Fq "$pattern" .dockerignore 2>/dev/null; then
    echo "OK: .dockerignore excludes ${pattern}"
  else
    echo "FAIL: .dockerignore missing ${pattern}"
    failures=$((failures + 1))
  fi
done

# Scan tracked/unignored names for secret-looking filenames (names only).
while IFS= read -r -d '' path; do
  base="$(basename "$path")"
  case "$base" in
    .env|*.pem|*.key|id_rsa|credentials.json|budget.sqlite3)
      # .env handled above via dockerignore; other names are hard fails if present
      if [[ "$base" != ".env" ]]; then
        echo "FAIL: secret-shaped filename in tree: ${base} (path redacted)"
        failures=$((failures + 1))
      fi
      ;;
  esac
done < <(find . -maxdepth 3 \( -name .git -o -name .venv -o -name artifacts \) -prune -o -type f -print0 2>/dev/null)

# Pattern presence in Dockerfile / compose (literal env names, not values).
if grep -RIn --include='Dockerfile' --include='docker-compose.yml' --include='docker-compose.*.yml' \
  -E 'DEEPSEEK_API_KEY[[:space:]]*=' . 2>/dev/null | grep -v '^\./docs/' >/dev/null; then
  echo "FAIL: DEEPSEEK_API_KEY assignment found in Docker/compose files"
  failures=$((failures + 1))
else
  echo "OK: no DEEPSEEK_API_KEY assignment in Docker/compose"
fi

if grep -q 'USER appuser' Dockerfile 2>/dev/null; then
  echo "OK: Dockerfile runs as non-root USER appuser"
else
  echo "FAIL: Dockerfile missing non-root USER appuser"
  failures=$((failures + 1))
fi

if [[ "$failures" -ne 0 ]]; then
  echo "== result: FAIL (${failures}) =="
  exit 1
fi

echo "== result: PASS =="
