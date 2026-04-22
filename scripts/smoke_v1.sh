#!/usr/bin/env bash
# v1.0.0 8-principles smoke test.
# Usage: bash scripts/smoke_v1.sh [BASE_URL]
# BASE_URL defaults to http://localhost:8080 (nginx).
# Self-signs a JWT inside the backend container using settings.SECRET_KEY.

set -u

BASE="${1:-http://localhost:8080}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

pass=0
fail=0
trap 'echo; echo "==== summary: ${pass} passed, ${fail} failed ===="; [ "$fail" -eq 0 ]' EXIT

ok()   { echo "  PASS $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL $1"; fail=$((fail+1)); }
head() { echo; echo "-- $1 --"; }

# ---------- self-sign admin JWT ----------
# Use sub=admin because backend ADMIN_USERNAMES defaults to 'admin'.
TOKEN=$(docker compose exec -T backend python -c "import jwt, datetime as dt; from app.config import settings; print(jwt.encode({'sub':'admin','exp':dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)}, settings.SECRET_KEY, 'HS256'))" | tr -d '\r\n')
if [ -z "$TOKEN" ]; then
  echo "FATAL: could not self-sign JWT"; exit 1
fi
AUTH=( -H "Authorization: Bearer $TOKEN" )

# ---------- 1. Backend /api/version reachable ----------
head "[1/8] backend /api/version"
v=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/version" || echo 000)
[ "$v" = "200" ] && ok "/api/version -> 200" || bad "/api/version -> $v"

# ---------- 2. Auth token accepted (protected endpoint) ----------
head "[2/8] auth (self-signed JWT accepted)"
p=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/api/projects" || echo 000)
[ "$p" = "200" ] && ok "/api/projects with JWT -> 200" || bad "/api/projects with JWT -> $p"

# ---------- 3. Admin usage quotas (Chunk 12) ----------
head "[3/8] admin usage quotas"
u=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/api/admin/usage?user_id=admin" || echo 000)
[ "$u" = "200" ] && ok "/api/admin/usage?user_id=admin -> 200" || bad "/api/admin/usage -> $u"

# ---------- 4. Export formats (Chunk 13) ----------
head "[4/8] export EPUB/PDF/DOCX"
PID="4fa4252d-5753-4112-9170-65fcc6e35c57"
for fmt in epub pdf docx; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/api/export/projects/${PID}.${fmt}" || echo 000)
  [ "$code" = "200" ] && ok "export .${fmt} -> 200" || bad "export .${fmt} -> $code"
done

# ---------- 5. Design tokens present in globals.css (Chunk 14) ----------
head "[5/8] design tokens"
GCSS="$REPO/frontend/src/app/globals.css"
if grep -q '@theme' "$GCSS" && grep -q -- '--color-brand-500' "$GCSS" && grep -q -- '--radius-card' "$GCSS"; then
  ok "@theme + brand-500 + radius-card tokens present"
else
  bad "design tokens missing from globals.css"
fi

# ---------- 6. i18n catalogs zh + en complete (Chunk 15) ----------
head "[6/8] i18n catalogs"
MSGS="$REPO/frontend/src/lib/i18n/messages.ts"
if grep -q '"app.name"' "$MSGS" && grep -q '"locale.en"' "$MSGS" && grep -q '"workspace.sidebar.collapse"' "$MSGS"; then
  ok "i18n messages include app.name + locale.en + workspace.sidebar.*"
else
  bad "i18n catalog keys missing"
fi

# ---------- 7. Mobile responsive primitives (Chunk 16) ----------
head "[7/8] mobile primitives"
LAYOUT="$REPO/frontend/src/app/layout.tsx"
if grep -q 'export const viewport' "$LAYOUT" && grep -q 'safe-area-x' "$GCSS"; then
  ok "viewport export + safe-area-x class present"
else
  bad "mobile primitives missing"
fi

# ---------- 8. Workspace sidebar collapsible (Chunk 17) ----------
head "[8/8] workspace sidebar"
WL="$REPO/frontend/src/components/workspace/WorkspaceLayout.tsx"
if grep -q 'sidebar-collapsed' "$WL" && grep -q 'panel-collapsed' "$WL" && grep -q 'usePersistedFlag' "$WL"; then
  ok "WorkspaceLayout has persisted collapse state"
else
  bad "WorkspaceLayout collapse state missing"
fi
