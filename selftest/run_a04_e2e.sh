#!/usr/bin/env bash
set -Eeuo pipefail

PARLEY="/home/ubuntu/proyectos/parley-c07-e2e"
NEA="/home/ubuntu/proyectos/nea-a04-e2e"
PG_CONTAINER="parley-a04-e2e-pg"
PG_VOLUME="parley_a04_e2e_pg"
PG_PORT="5434"
APP_PORT="3108"
PG_USER="parley_a04"
PG_PASS="parley_a04_20260921"
PG_DB="parley_a04_e2e"

APP_PID=""
COOKIE="$(mktemp)"

cleanup() {
  set +e
  if [ -n "${APP_PID:-}" ] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null
    wait "$APP_PID" 2>/dev/null
  fi
  docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$PG_VOLUME" >/dev/null 2>&1 || true
  rm -f "$COOKIE"
}
trap cleanup EXIT INT TERM

NODE_BIN="$(find /home/ubuntu/.nvm/versions/node -type f -path '*/bin/node' 2>/dev/null | sort -V | tail -n1)"
[ -n "$NODE_BIN" ] || { echo "ERROR: Node no encontrado"; exit 1; }
export PATH="$(dirname "$NODE_BIN"):$PATH"
export NODE_OPTIONS="--max-old-space-size=4096"
echo "Node: $(node --version)"

echo "===== PARLEY MAIN AISLADO ====="
git -c safe.directory="$PARLEY" -C "$PARLEY" fetch origin
git -c safe.directory="$PARLEY" -C "$PARLEY" checkout --detach origin/main
git -c safe.directory="$PARLEY" -C "$PARLEY" reset --hard origin/main
echo "PARLEY_HEAD=$(git -c safe.directory="$PARLEY" -C "$PARLEY" rev-parse HEAD)"

echo "===== NEA A04 ====="
if [ ! -d "$NEA/.git" ]; then
  timeout 2m git clone https://github.com/djmu677/nea-agent.git "$NEA"
fi
git -c safe.directory="$NEA" -C "$NEA" fetch origin
if git -c safe.directory="$NEA" -C "$NEA" show-ref --verify --quiet refs/heads/feature/a04-agenda-tools-nea; then
  git -c safe.directory="$NEA" -C "$NEA" switch feature/a04-agenda-tools-nea
else
  git -c safe.directory="$NEA" -C "$NEA" switch --track -c feature/a04-agenda-tools-nea origin/feature/a04-agenda-tools-nea
fi
git -c safe.directory="$NEA" -C "$NEA" merge --ff-only origin/feature/a04-agenda-tools-nea
echo "NEA_HEAD=$(git -c safe.directory="$NEA" -C "$NEA" rev-parse HEAD)"

echo "===== POSTGRES A04 ====="
docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
docker volume rm "$PG_VOLUME" >/dev/null 2>&1 || true
docker volume create "$PG_VOLUME" >/dev/null
docker run -d   --name "$PG_CONTAINER"   -e POSTGRES_USER="$PG_USER"   -e POSTGRES_PASSWORD="$PG_PASS"   -e POSTGRES_DB="$PG_DB"   -p "127.0.0.1:${PG_PORT}:5432"   -v "${PG_VOLUME}:/var/lib/postgresql/data"   postgres:16-alpine >/dev/null

for i in $(seq 1 60); do
  if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    echo "PostgreSQL listo."
    break
  fi
  [ "$i" -lt 60 ] || { echo "ERROR: PostgreSQL no quedó listo"; exit 1; }
  sleep 1
done

export APP_BASE_URL="http://127.0.0.1:${APP_PORT}"
export DATABASE_URL="postgresql://${PG_USER}:${PG_PASS}@127.0.0.1:${PG_PORT}/${PG_DB}"
export POSTGRES_PASSWORD="$PG_PASS"
export BETTER_AUTH_SECRET="$(openssl rand -hex 32)"
export ENCRYPTION_KEY="$(openssl rand -base64 32)"
export META_WEBHOOK_VERIFY_TOKEN="$(openssl rand -hex 32)"
export BOT_API_KEY="$(openssl rand -hex 32)"
export META_GRAPH_API_VERSION="v25.0"
export META_GRAPH_BASE_URL="${APP_BASE_URL}/api/dev/wa-mock/graph"
export WA_MOCK_ENABLED="true"
export ALLOW_SIGNUP="true"
export AGENDA="on"
export AGENT_COALESCE_MS="0"
export OPENROUTER_API_TOKEN="test-token"
export OPENROUTER_BASE_URL="${APP_BASE_URL}/api/dev/ai-mock"
export MEDIA_DIR="${PARLEY}/.dev-media"
export NODE_ENV="development"

echo "===== MIGRACIONES ====="
cd "$PARLEY"
timeout 3m ./node_modules/.bin/drizzle-kit migrate

echo "===== PARLEY :${APP_PORT} ====="
./node_modules/.bin/next dev -H 127.0.0.1 -p "$APP_PORT" >/tmp/parley-a04-app.log 2>&1 &
APP_PID=$!

for i in $(seq 1 120); do
  if curl --max-time 3 -fsS "${APP_BASE_URL}/api/health" >/dev/null 2>&1; then
    echo "Parley listo."
    break
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "ERROR: Parley terminó."
    tail -n 120 /tmp/parley-a04-app.log
    exit 1
  fi
  [ "$i" -lt 120 ] || { echo "ERROR: timeout esperando Parley"; tail -n 120 /tmp/parley-a04-app.log; exit 1; }
  sleep 1
done

echo "===== ORGANIZACIÓN E2E ====="
curl --max-time 20 -fsS -c "$COOKIE"   -H "content-type: application/json"   -X POST "${APP_BASE_URL}/api/auth/sign-up/email"   -d '{"email":"a04-e2e@parley.test","password":"password-a04-e2e-123","name":"Operador A04 E2E"}' >/dev/null

curl --max-time 20 -fsS -b "$COOKIE" -c "$COOKIE"   -H "content-type: application/json"   -X PUT "${APP_BASE_URL}/api/settings/whatsapp"   -d '{"wabaId":"WABA-A04-E2E","phoneNumberId":"PN-A04-E2E","token":"tok-a04-e2e"}' >/dev/null

crear_conversacion() {
  local identity="$1"
  local name="$2"
  curl --max-time 20 -fsS     -H "content-type: application/json"     -X POST "${APP_BASE_URL}/api/dev/wa-mock/inbound"     -d "{\"phoneNumberId\":\"PN-A04-E2E\",\"from\":\"${identity}\",\"name\":\"${name}\",\"text\":\"Hola, quiero agendar una cita\",\"waMessageId\":\"wamid.a04.${identity}\"}" >/dev/null

  for i in $(seq 1 30); do
    local json cid
    json="$(curl --max-time 5 -sS -H "X-API-Key: ${BOT_API_KEY}" "${APP_BASE_URL}/api/bot/context?waIdentity=${identity}" || true)"
    cid="$(printf '%s' "$json" | python3 -c 'import json,sys
try:
 print(json.load(sys.stdin).get("conversation",{}).get("id",""))
except Exception:
 pass' 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      printf '%s\n' "$cid"
      return 0
    fi
    sleep 1
  done
  echo "ERROR: conversación no creada para ${identity}" >&2
  return 1
}

C1="$(crear_conversacion 56990000001 'Cliente A04 Uno')"
C2="$(crear_conversacion 56990000002 'Cliente A04 Dos')"
echo "C1=$C1"
echo "C2=$C2"

echo "===== E2E NEA -> PARLEY ====="
timeout 6m docker run --rm   --network host   -e PYTHONDONTWRITEBYTECODE=1   -e CRM_BASE_URL="$APP_BASE_URL"   -e CRM_BOT_API_KEY="$BOT_API_KEY"   -e A04_CONVERSATION_1="$C1"   -e A04_CONVERSATION_2="$C2"   -v "$NEA:/work:ro"   -w /work   python:3.11-slim   sh -lc 'pip install --disable-pip-version-check -q -r requirements-dev.txt && python selftest/a04_agenda.py'

echo "===== A04 FINAL ====="
echo "E2E=OK"
