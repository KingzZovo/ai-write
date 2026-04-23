#!/usr/bin/env bash
# v1.2.0 smoke test (B series: observability + CI).
# Usage:
#   bash scripts/smoke_v1.sh [BASE_URL]
#     BASE_URL defaults to http://localhost:8080 (nginx).
#
# Modes:
#   SMOKE_STATIC_ONLY=1    skip every block that requires a running backend,
#                          docker compose exec, /metrics curl, or prometheus
#                          query. Only static grep-based assertions run.
#                          Designed for GitHub Actions CI where no service
#                          containers are up.
#
# Self-signs a JWT inside the backend container using settings.SECRET_KEY
# (skipped in static-only mode).

set -u

BASE="${1:-http://localhost:8080}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

SMOKE_STATIC_ONLY="${SMOKE_STATIC_ONLY:-0}"

pass=0
fail=0
skipped=0
trap 'echo; echo "==== summary: ${pass} passed, ${fail} failed, ${skipped} skipped ===="; [ "$fail" -eq 0 ]' EXIT

ok()   { echo "  PASS $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL $1"; fail=$((fail+1)); }
skip() { echo "  SKIP $1"; skipped=$((skipped+1)); }
head() { echo; echo "-- $1 --"; }

# True when we have a live backend + docker compose stack available.
is_runtime() { [ "$SMOKE_STATIC_ONLY" != "1" ]; }

# ---------- self-sign admin JWT (runtime only) ----------
TOKEN=""
AUTH=()
if is_runtime; then
  TOKEN=$(docker compose exec -T backend python -c "import jwt, datetime as dt; from app.config import settings; print(jwt.encode({'sub':'admin','exp':dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)}, settings.SECRET_KEY, 'HS256'))" | tr -d '\r\n')
  if [ -z "$TOKEN" ]; then
    echo "FATAL: could not self-sign JWT"; exit 1
  fi
  AUTH=( -H "Authorization: Bearer $TOKEN" )
fi

# ---------- 1. Backend /api/version reachable ----------
head "[1/8] backend /api/version"
if is_runtime; then
  v=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/version" || echo 000)
  [ "$v" = "200" ] && ok "/api/version -> 200" || bad "/api/version -> $v"
else
  skip "/api/version (static-only mode)"
fi

# ---------- 2. Auth token accepted (protected endpoint) ----------
head "[2/8] auth (self-signed JWT accepted)"
if is_runtime; then
  p=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/api/projects" || echo 000)
  [ "$p" = "200" ] && ok "/api/projects with JWT -> 200" || bad "/api/projects with JWT -> $p"
else
  skip "/api/projects JWT auth (static-only mode)"
fi

# ---------- 3. Admin usage quotas (Chunk 12) ----------
head "[3/8] admin usage quotas"
if is_runtime; then
  u=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/api/admin/usage?user_id=admin" || echo 000)
  [ "$u" = "200" ] && ok "/api/admin/usage?user_id=admin -> 200" || bad "/api/admin/usage -> $u"
else
  skip "/api/admin/usage (static-only mode)"
fi

# ---------- 4. Export formats (Chunk 13) ----------
head "[4/8] export EPUB/PDF/DOCX"
if is_runtime; then
  PID="4fa4252d-5753-4112-9170-65fcc6e35c57"
  for fmt in epub pdf docx; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/api/export/projects/${PID}.${fmt}" || echo 000)
    [ "$code" = "200" ] && ok "export .${fmt} -> 200" || bad "export .${fmt} -> $code"
  done
else
  skip "export epub/pdf/docx (static-only mode)"
fi

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
# chunk-23: per-project key composition + [/] shortcut + mobile auto-collapse.
DWS="$REPO/frontend/src/components/workspace/DesktopWorkspace.tsx"
if grep -q 'composeKey' "$WL" && grep -q 'projectId' "$WL" && grep -q "base}:\${projectId" "$WL"; then
  ok "WorkspaceLayout composes per-project storage key from projectId"
else
  bad "WorkspaceLayout per-project key composition missing"
fi
if grep -q 'projectId={currentProject?.id}' "$DWS"; then
  ok "DesktopWorkspace passes currentProject?.id into WorkspaceLayout"
else
  bad "DesktopWorkspace does not wire projectId prop"
fi
if grep -q "e.key === '\\['" "$WL" && grep -q "e.key === '\\]'" "$WL" && grep -q 'max-width: 767px' "$WL"; then
  ok "WorkspaceLayout wires [ / ] shortcuts + <768px auto-collapse"
else
  bad "WorkspaceLayout shortcut / auto-collapse missing"
fi

# ---------- 9. i18n language switcher + cookie (Chunk 20) ----------
head "[9/9] i18n language switcher + cookie"
SET="$REPO/frontend/src/app/settings/page.tsx"
PROV="$REPO/frontend/src/lib/i18n/I18nProvider.tsx"
MSGS="$REPO/frontend/src/lib/i18n/messages.ts"
if grep -q 'language-switcher' "$SET" && grep -q 'useLocale' "$SET" && grep -q 'ai-write-locale' "$PROV" && grep -q 'settings.preferences.language' "$MSGS"; then
  ok "settings page wires LanguageSwitcher + cookie ai-write-locale + en catalog"
else
  bad "language switcher or cookie assertion missing"
fi

# ---------- 10. Mobile landing: project list + outline drawer + nav hamburger (Chunk 21) ----------
head "[10/10] mobile landing"
PLP="$REPO/frontend/src/components/project/ProjectListPage.tsx"
MWS="$REPO/frontend/src/components/workspace/MobileWorkspace.tsx"
NAV="$REPO/frontend/src/components/Navbar.tsx"
if grep -q 'project-list-grid' "$PLP" && grep -q 'grid-cols-1 md:grid-cols-2' "$PLP" && grep -q 'px-3 md:px-6' "$PLP"; then
  ok "ProjectListPage: single-col default + mobile padding + data-testid hook"
else
  bad "ProjectListPage mobile-landing markers missing"
fi
if grep -q 'mobile-outline-drawer' "$MWS" && grep -q 'mobile-outline-toggle' "$MWS"; then
  ok "MobileWorkspace: outline drawer + toggle present"
else
  bad "MobileWorkspace outline drawer markers missing"
fi
if grep -q 'nav-hamburger' "$NAV" && grep -q 'nav-mobile-drawer' "$NAV"; then
  ok "Navbar: hamburger + mobile drawer wired"
else
  bad "Navbar hamburger/drawer markers missing"
fi

# ---------- 11. Design-token migration: no hex/rgba in business UI (Chunk 22) ----------
head "[11/11] design-token migration"
# Whitelist: globals.css is the token source; graph-palette.ts is the documented
# data-viz mirror; layout.tsx themeColor meta tag; lib/graph-palette.ts import lines.
hits=$(grep -RIl -E '#[0-9a-fA-F]{6}\b|rgba?\(' "$REPO/frontend/src" 2>/dev/null \
  | grep -vE '(app/globals\.css|lib/graph-palette\.ts|app/layout\.tsx)$' || true)
if [ -z "$hits" ]; then
  ok "no hex/rgba literals outside tokens + graph-palette + layout themeColor"
else
  bad "stray hex/rgba literals found in: $(echo "$hits" | tr '\n' ' ')"
fi
# EditorView.tsx ProseMirror style must use CSS vars
EV="$REPO/frontend/src/components/editor/EditorView.tsx"
if grep -q 'var(--text)' "$EV" && grep -q 'var(--color-info-500)' "$EV"; then
  ok "EditorView style uses --text + --color-info-500 tokens"
else
  bad "EditorView style tokens missing"
fi
# WritingGuidePanel active card uses shadow-card
WG="$REPO/frontend/src/components/panels/WritingGuidePanel.tsx"
if grep -q 'shadow-card' "$WG"; then
  ok "WritingGuidePanel active card uses shadow-card"
else
  bad "WritingGuidePanel shadow-card missing"
fi

# ---------- 12. Backend structured JSON logging (Chunk 24) ----------
head "[12/12] backend structured logging"
LOGPY="$REPO/backend/app/observability/logging.py"
REQMW="$REPO/backend/app/middlewares/request_logging.py"
MAIN="$REPO/backend/app/main.py"
if grep -q 'from loguru import logger' "$LOGPY" && grep -q 'def setup_logging' "$LOGPY" && grep -q '_json_sink' "$LOGPY" && grep -q '_SENSITIVE_KEYS' "$LOGPY"; then
  ok "observability/logging.py: loguru imported + setup_logging + JSON sink + redactor"
else
  bad "observability/logging.py missing loguru/setup_logging/JSON sink"
fi
if grep -q 'class RequestLoggingMiddleware' "$REQMW" && grep -q 'X-Request-ID' "$REQMW" && grep -q 'log_http_request' "$REQMW"; then
  ok "middlewares/request_logging.py: RequestLoggingMiddleware + X-Request-ID + http log"
else
  bad "request_logging middleware missing"
fi
if grep -q 'setup_logging()' "$MAIN" && grep -q 'RequestLoggingMiddleware' "$MAIN"; then
  ok "main.py wires setup_logging() + RequestLoggingMiddleware"
else
  bad "main.py does not wire structured logging"
fi
if is_runtime; then
  # runtime: loguru importable inside the backend container + sink attached.
  rt=$(docker compose exec -T backend python -c "from app.observability.logging import setup_logging, is_configured; setup_logging(); import sys; sys.stderr.write('__SMOKE_LOG_OK__\n' if is_configured() else '__SMOKE_LOG_NO__\n')" 2>&1 >/dev/null | tr -d '\r')
  if echo "$rt" | grep -q '__SMOKE_LOG_OK__'; then
    ok "backend runtime: loguru JSON sink mounted after setup_logging"
  else
    bad "backend runtime structured logging not initialized ($rt)"
  fi
  # runtime: X-Request-ID echoed back on /api/version response header.
  rid=$(curl -sSI "$BASE/api/version" 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /^x-request-id:/ {print $2}' | tr -d '\r\n')
  if [ -n "$rid" ]; then
    ok "/api/version response includes X-Request-ID header ($rid)"
  else
    bad "/api/version response missing X-Request-ID header"
  fi
else
  skip "loguru runtime check + X-Request-ID echo (static-only mode)"
fi

# ---------- 13. Prometheus metrics + dashboard (Chunk 25) ----------
head "[13/13] prometheus metrics"
METRICS_PY="$REPO/backend/app/observability/metrics.py"
TASKS_PY="$REPO/backend/app/tasks/__init__.py"
if grep -q 'http_requests_total' "$METRICS_PY" && grep -q 'CELERY_TASK_TOTAL' "$METRICS_PY" && grep -q 'CELERY_TASK_DURATION' "$METRICS_PY" && grep -q 'DB_POOL_SIZE' "$METRICS_PY" && grep -q '_refresh_db_pool_gauges' "$METRICS_PY"; then
  ok "observability/metrics.py: http_requests_total + celery + db_pool collectors"
else
  bad "observability/metrics.py missing one of: http_requests_total / CELERY_* / DB_POOL_*"
fi
if grep -q 'task_prerun' "$TASKS_PY" && grep -q 'task_postrun' "$TASKS_PY" && grep -q 'CELERY_TASK_DURATION' "$TASKS_PY"; then
  ok "tasks/__init__.py wires celery signals to Prometheus counters"
else
  bad "tasks/__init__.py missing celery signal instrumentation"
fi
if is_runtime; then
  # /metrics endpoint reachable + exposes the renamed counter + new families.
  METRICS_URL="http://127.0.0.1:8000/metrics"
  m_code=$(curl -sS -o /tmp/_metrics.out -w '%{http_code}' "$METRICS_URL")
  if [ "$m_code" = "200" ]; then
    ok "/metrics -> 200"
  else
    bad "/metrics -> $m_code"
  fi
  if grep -q '^# HELP http_requests_total' /tmp/_metrics.out; then
    ok "/metrics exposes http_requests_total"
  else
    bad "/metrics missing http_requests_total"
  fi
  if grep -q '^# HELP celery_task_total' /tmp/_metrics.out && grep -q '^# HELP celery_task_duration_seconds' /tmp/_metrics.out; then
    ok "/metrics exposes celery_task_total + celery_task_duration_seconds"
  else
    bad "/metrics missing celery task families"
  fi
  if grep -q '^db_pool_size{pool="main"}' /tmp/_metrics.out; then
    ok "/metrics exposes db_pool_size for main pool"
  else
    bad "/metrics missing db_pool_size{pool=main}"
  fi
  # prometheus + grafana containers are running.
  if docker compose ps --status running --services 2>/dev/null | grep -qx prometheus; then
    ok "prometheus container running"
  else
    bad "prometheus container not running"
  fi
  if docker compose ps --status running --services 2>/dev/null | grep -qx grafana; then
    ok "grafana container running"
  else
    bad "grafana container not running"
  fi
  # prometheus is actually scraping the backend.
  sleep 1
  PROM_URL="http://127.0.0.1:9091/api/v1/query"
  PROM_QUERY='up{job="ai-write-backend"}'
  pq=$(curl -sS --get --data-urlencode "query=$PROM_QUERY" "$PROM_URL" 2>/dev/null)
  if echo "$pq" | grep -q '"value":\[.*"1"\]'; then
    ok "prometheus reports up{job=ai-write-backend} == 1"
  else
    bad "prometheus does not yet report backend as up ($(echo "$pq" | head -c 200))"
  fi
else
  skip "/metrics + prometheus container + prom query (static-only mode)"
fi

# ---------- 14. Sentry wiring (Chunk 26) ----------
head "[14/14] sentry wiring"
SENTRY_PY="$REPO/backend/app/observability/sentry_init.py"
SENTRY_TS="$REPO/frontend/src/sentry.client.config.ts"
if grep -q 'before_send=_scrub_event' "$SENTRY_PY" && grep -q 'def _scrub_event' "$SENTRY_PY" && grep -q 'redact' "$SENTRY_PY"; then
  ok "sentry_init.py wires before_send=_scrub_event using redact()"
else
  bad "sentry_init.py missing before_send/redact wiring"
fi
if is_runtime; then
  # Backend runtime: sentry_sdk importable inside the container.
  sv=$(docker compose exec -T backend python -c "import sentry_sdk, sys; sys.stderr.write('__SMOKE_SDK_OK__ ' + sentry_sdk.VERSION + '\n')" 2>&1 >/dev/null | tr -d '\r')
  if echo "$sv" | grep -q '__SMOKE_SDK_OK__'; then
    ok "backend runtime: sentry_sdk importable ($(echo "$sv" | awk '/__SMOKE_SDK_OK__/ {print $2}'))"
  else
    bad "backend sentry_sdk import failed ($sv)"
  fi
  # Backend runtime: init_sentry no-op without DSN (must not throw, must return False).
  iv=$(docker compose exec -T backend python -c "import os; os.environ.pop('SENTRY_DSN', None); from app.observability.sentry_init import init_sentry; import sys; sys.stderr.write('__SMOKE_INIT_RET__ ' + str(init_sentry(component='smoke-test')) + '\n')" 2>&1 >/dev/null | tr -d '\r')
  if echo "$iv" | grep -q '__SMOKE_INIT_RET__ False'; then
    ok "init_sentry() silently returns False when SENTRY_DSN unset"
  else
    bad "init_sentry() did not silent-skip without DSN ($iv)"
  fi
else
  skip "sentry_sdk runtime import + init_sentry no-op (static-only mode)"
fi
# Frontend client config exists + DSN env var name + initClientSentry export.
if [ -f "$SENTRY_TS" ] && grep -q 'NEXT_PUBLIC_SENTRY_DSN' "$SENTRY_TS" && grep -q 'initClientSentry' "$SENTRY_TS" && grep -q '@sentry/browser' "$SENTRY_TS"; then
  ok "frontend sentry.client.config.ts present + DSN gated + dynamic @sentry/browser shim"
else
  bad "frontend sentry.client.config.ts missing or incomplete"
fi

# ---------- 15. GitHub Actions CI wiring (Chunk 27) ----------
head "[15/15] github actions CI"
CI_YML="$REPO/.github/workflows/ci.yml"
JWT_FIX="$REPO/backend/tests/fixtures/self_sign_jwt.py"
if [ -f "$CI_YML" ]; then
  ok ".github/workflows/ci.yml present"
else
  bad ".github/workflows/ci.yml missing"
fi
if grep -q 'pull_request:' "$CI_YML" 2>/dev/null; then
  ok "ci.yml triggers on pull_request"
else
  bad "ci.yml does not trigger on pull_request"
fi
if grep -q 'ruff check' "$CI_YML" 2>/dev/null && grep -q 'mypy' "$CI_YML" 2>/dev/null && grep -q 'pytest' "$CI_YML" 2>/dev/null; then
  ok "ci.yml runs ruff + mypy + pytest on backend"
else
  bad "ci.yml missing ruff/mypy/pytest wiring"
fi
if grep -q 'next build' "$CI_YML" 2>/dev/null; then
  ok "ci.yml runs next build on frontend"
else
  bad "ci.yml does not run next build"
fi
if grep -q 'smoke_v1.sh' "$CI_YML" 2>/dev/null && grep -q 'SMOKE_STATIC_ONLY' "$CI_YML" 2>/dev/null; then
  ok "ci.yml invokes smoke_v1.sh with SMOKE_STATIC_ONLY subset"
else
  bad "ci.yml does not invoke smoke_v1.sh non-network subset"
fi
if [ -f "$JWT_FIX" ] && grep -q 'def sign_smoke_jwt' "$JWT_FIX" 2>/dev/null; then
  ok "tests/fixtures/self_sign_jwt.py present with sign_smoke_jwt helper"
else
  bad "tests/fixtures/self_sign_jwt.py missing or lacks sign_smoke_jwt"
fi

# ---------- 16. v1.3.0 target_word_count schema (chunk-28) ----------
head "[16/16] v1.3.0 target_word_count schema"
MIG_V13="backend/alembic/versions/a1001300_v13_target_word_count.py"
MODEL_PY="backend/app/models/project.py"
if [ -f "$MIG_V13" ] && grep -q 'revision = "a1001300"' "$MIG_V13" 2>/dev/null; then
  ok "alembic revision a1001300_v13_target_word_count.py present"
else
  bad "alembic revision a1001300 missing or malformed"
fi
if grep -q 'down_revision = "a1001200"' "$MIG_V13" 2>/dev/null; then
  ok "a1001300 chains onto a1001200"
else
  bad "a1001300 does not chain onto a1001200"
fi
if grep -q 'target_word_count' "$MODEL_PY" 2>/dev/null \
    && ! grep -q 'target_words = Column' "$MODEL_PY" 2>/dev/null; then
  ok "Chapter.target_words renamed to target_word_count in ORM"
else
  bad "Chapter model still has legacy target_words column"
fi
if grep -c 'target_word_count = Column' "$MODEL_PY" 2>/dev/null | grep -q '^3$'; then
  ok "Project/Volume/Chapter all carry target_word_count ORM field"
else
  bad "Project/Volume/Chapter ORM missing target_word_count (expected 3 occurrences)"
fi
if is_runtime; then
  ALEMBIC_CUR=$(docker compose exec -T backend alembic current 2>/dev/null | awk '/head/ {print $1; exit}')
  if [ "$ALEMBIC_CUR" = "a1001300" ]; then
    ok "alembic current head = a1001300"
  else
    bad "alembic current head != a1001300 (got: '$ALEMBIC_CUR')"
  fi
  PG_OUT=$(docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -tAc "SELECT table_name || '.' || column_name || ':' || is_nullable || ':' || COALESCE(column_default,'') FROM information_schema.columns WHERE table_name IN ('projects','volumes','chapters') AND column_name = 'target_word_count' ORDER BY table_name" 2>/dev/null)
  if echo "$PG_OUT" | grep -q '^projects.target_word_count:NO:3000000'; then
    ok "projects.target_word_count column NOT NULL default 3000000"
  else
    bad "projects.target_word_count shape wrong: $(echo "$PG_OUT" | grep '^projects')"
  fi
  if echo "$PG_OUT" | grep -q '^volumes.target_word_count:NO:200000'; then
    ok "volumes.target_word_count column NOT NULL default 200000"
  else
    bad "volumes.target_word_count shape wrong: $(echo "$PG_OUT" | grep '^volumes')"
  fi
  if echo "$PG_OUT" | grep -q '^chapters.target_word_count:NO:50000'; then
    ok "chapters.target_word_count column NOT NULL default 50000"
  else
    bad "chapters.target_word_count shape wrong: $(echo "$PG_OUT" | grep '^chapters')"
  fi
  LEGACY=$(docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -tAc "SELECT 1 FROM information_schema.columns WHERE table_name='chapters' AND column_name='target_words'" 2>/dev/null | tr -d '[:space:]')
  if [ -z "$LEGACY" ]; then
    ok "chapters.target_words legacy column removed (renamed)"
  else
    bad "chapters.target_words legacy column still present"
  fi
else
  skip "alembic head check (static-only mode)"
  skip "projects.target_word_count column shape (static-only mode)"
  skip "volumes.target_word_count column shape (static-only mode)"
  skip "chapters.target_word_count column shape (static-only mode)"
  skip "chapters.target_words legacy column removal (static-only mode)"
fi

# ---------- 17. v1.3.0 budget allocator (chunk-29) ----------
head "[17/17] v1.3.0 budget allocator"
ALLOC_PY="backend/app/services/budget_allocator.py"
ALLOC_TEST="backend/tests/services/test_budget_allocator.py"
PROJ_API="backend/app/api/projects.py"
if [ -f "$ALLOC_PY" ] && grep -q 'def allocate_even' "$ALLOC_PY" && grep -q 'def allocate_project_budget' "$ALLOC_PY"; then
  ok "budget_allocator.py: allocate_even + allocate_project_budget present"
else
  bad "budget_allocator.py missing or incomplete"
fi
if [ -f "$ALLOC_TEST" ] && grep -q 'def test_' "$ALLOC_TEST"; then
  ok "test_budget_allocator.py present with unit tests"
else
  bad "test_budget_allocator.py missing"
fi
if grep -q '/allocate-budget' "$PROJ_API" && grep -q 'allocate_project_budget' "$PROJ_API"; then
  ok "projects.py exposes POST /{project_id}/allocate-budget wired to allocator"
else
  bad "projects.py missing /allocate-budget endpoint or allocator wiring"
fi
if is_runtime; then
  # Try host pytest first (backend image does not ship pytest). Fallback to
  # in-container python -m pytest if the host has no pytest on PATH.
  if command -v pytest >/dev/null 2>&1; then
    rt=$(cd "$REPO" && PYTHONPATH=backend pytest -q --no-header --noconftest -p no:cacheprovider backend/tests/services/test_budget_allocator.py 2>&1 | tr -d '\r')
  else
    rt=$(docker compose exec -T backend python -m pytest -q --no-header --noconftest -p no:cacheprovider backend/tests/services/test_budget_allocator.py 2>&1 | tr -d '\r')
  fi
  if echo "$rt" | awk '/[0-9]+ passed/ { found=1; exit } END { exit !found }'; then
    npass=$(echo "$rt" | grep -oE '[0-9]+ passed' | head -n1 | awk '{print $1}')
    ok "pytest test_budget_allocator.py: ${npass:-?} passed"
  else
    bad "pytest test_budget_allocator.py failed ($(echo "$rt" | tail -3 | tr '\n' ' '))"
  fi
  NP=$(curl -sS -X POST "${AUTH[@]}" -H "Content-Type: application/json" -d '{"title":"chunk29-smoke","genre":"test","premise":"budget allocator smoke"}' "$BASE/api/projects" 2>/dev/null)
  PID=$(echo "$NP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null)
  if [ -n "$PID" ]; then
    for i in 1 2 3; do
      curl -sS -X POST "${AUTH[@]}" -H "Content-Type: application/json" -d "{\"title\":\"vol$i\",\"volume_idx\":$i}" "$BASE/api/projects/$PID/volumes" >/dev/null 2>&1
    done
    plan=$(curl -sS -X POST "${AUTH[@]}" "$BASE/api/projects/$PID/allocate-budget?force=true" 2>/dev/null)
    vsum=$(echo "$plan" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('volume_sum',0))" 2>/dev/null)
    ptot=$(echo "$plan" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('project_total',0))" 2>/dev/null)
    if [ -n "$vsum" ] && [ "$vsum" = "$ptot" ] && [ "$ptot" != "0" ]; then
      ok "allocate-budget: volume_sum == project_total ($vsum)"
    else
      bad "allocate-budget: volume_sum=$vsum project_total=$ptot"
    fi
    psum=$(docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -tAc "SELECT COALESCE(SUM(target_word_count),0) FROM volumes WHERE project_id='$PID'" 2>/dev/null | tr -d '[:space:]')
    if [ "$psum" = "$ptot" ] && [ -n "$psum" ]; then
      ok "psql SUM(volumes.target_word_count) == project_total ($psum)"
    else
      bad "psql volumes sum=$psum project_total=$ptot"
    fi
    curl -sS -X DELETE "${AUTH[@]}" "$BASE/api/projects/$PID?purge=true" >/dev/null 2>&1 || true
  else
    bad "could not create smoke project for allocator runtime test"
  fi
else
  skip "pytest test_budget_allocator.py (static-only mode)"
  skip "allocate-budget endpoint roundtrip (static-only mode)"
  skip "psql volumes sum verification (static-only mode)"
fi

# ---------- 18. v1.3.0 regenerate-volume budget auto-wire (chunk-30) ----------
head "[18/18] v1.3.0 regenerate_volume budget auto-wire"
VOL_API="backend/app/api/volumes.py"
REGEN_TEST="backend/tests/services/test_regenerate_budget_flow.py"
if grep -q 'from app.services.budget_allocator import allocate_even' "$VOL_API"; then
  ok "volumes.py imports allocate_even"
else
  bad "volumes.py does NOT import allocate_even"
fi
if grep -q 'allocate_even(volume_target' "$VOL_API" && grep -q 'chapter_word_counts' "$VOL_API"; then
  ok "regenerate_volume wires allocate_even into new chapters + SSE done event"
else
  bad "regenerate_volume not wired to allocator (allocate_even call or chapter_word_counts missing)"
fi
if [ -f "$REGEN_TEST" ] && grep -q 'def test_regenerate_' "$REGEN_TEST"; then
  ok "test_regenerate_budget_flow.py present with regression tests"
else
  bad "test_regenerate_budget_flow.py missing or empty"
fi
if is_runtime; then
  # Run the regenerate unit tests on the host (backend container lacks pytest).
  if command -v pytest >/dev/null 2>&1; then
    rt=$(cd "$REPO" && PYTHONPATH=backend pytest -q --no-header --noconftest -p no:cacheprovider backend/tests/services/test_regenerate_budget_flow.py 2>&1 | tr -d '\r')
  else
    rt=$(docker compose exec -T backend python -m pytest -q --no-header --noconftest -p no:cacheprovider backend/tests/services/test_regenerate_budget_flow.py 2>&1 | tr -d '\r')
  fi
  if echo "$rt" | awk '/[0-9]+ passed/ { found=1; exit } END { exit !found }'; then
    npass=$(echo "$rt" | grep -oE '[0-9]+ passed' | head -n1 | awk '{print $1}')
    ok "pytest test_regenerate_budget_flow.py: ${npass:-?} passed"
  else
    bad "pytest test_regenerate_budget_flow.py failed ($(echo "$rt" | tail -3 | tr '\n' ' '))"
  fi
else
  skip "pytest test_regenerate_budget_flow.py (static-only mode)"
fi

# ---------- 19. v1.3.0 budget-status read-only audit (chunk-31) ----------
head "[19/19] v1.3.0 GET /projects/{id}/budget-status"
PROJ_API="backend/app/api/projects.py"
if grep -q '@router.get("/{project_id}/budget-status")' "$PROJ_API"; then
  ok "projects.py exposes GET /{project_id}/budget-status"
else
  bad "projects.py missing GET /{project_id}/budget-status"
fi
if grep -q 'volumes_drift' "$PROJ_API" && grep -q 'chapters_drift' "$PROJ_API" && grep -q 'per_volume' "$PROJ_API"; then
  ok "budget-status shape includes volumes_drift + chapters_drift + per_volume"
else
  bad "budget-status shape missing drift/per_volume keys"
fi
if is_runtime; then
  # Create a fresh project, add 3 volumes, then curl budget-status; assert healthy.
  bs_pid=$(curl -sS "${AUTH[@]}" -H 'content-type: application/json' \
    -X POST "$BASE/api/projects" -d '{"title":"bs_smoke","target_word_count":600000}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
  if [ -n "$bs_pid" ]; then
    for vi in 1 2 3; do
      curl -sS -o /dev/null "${AUTH[@]}" -H 'content-type: application/json' \
        -X POST "$BASE/api/projects/$bs_pid/volumes" \
        -d "{\"title\":\"V$vi\",\"volume_idx\":$vi}"
    done
    # Trigger allocator so volumes sum to 600000 exactly.
    curl -sS -o /dev/null "${AUTH[@]}" -X POST "$BASE/api/projects/$bs_pid/allocate-budget?force=true"
    bs_json=$(curl -sS "${AUTH[@]}" "$BASE/api/projects/$bs_pid/budget-status")
    vd=$(echo "$bs_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes_drift"])' 2>/dev/null)
    vh=$(echo "$bs_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes_healthy"])' 2>/dev/null)
    vc=$(echo "$bs_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["volume_count"])' 2>/dev/null)
    if [ "$vd" = "0" ] && [ "$vh" = "True" ] && [ "$vc" = "3" ]; then
      ok "budget-status: 3 volumes sum to project_total, drift=0, healthy"
    else
      bad "budget-status unhealthy: vd=$vd vh=$vh vc=$vc body=$(echo "$bs_json" | head -c 200)"
    fi
    # 404 path: non-existent project
    code=$(curl -sS -o /dev/null -w '%{http_code}' "${AUTH[@]}" \
      "$BASE/api/projects/00000000-0000-0000-0000-000000000000/budget-status")
    if [ "$code" = "404" ]; then
      ok "budget-status returns 404 for missing project"
    else
      bad "budget-status missing-project expected 404, got $code"
    fi
  else
    bad "budget-status: could not create smoke project"
  fi
else
  skip "budget-status runtime curl (static-only mode)"
  skip "budget-status 404 path (static-only mode)"
fi
