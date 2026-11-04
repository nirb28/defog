#!/usr/bin/env bash
# Deploy the Defog API to Google Cloud Run. Safe to re-run: it updates what's there.
#
# Run it in Google Cloud Shell (it has gcloud and Docker-free builds), from backend/:
#
#   DATABASE_URL='postgresql://...' bash deploy/cloudrun.sh
#
# Optional variables:
#   PROJECT      Google Cloud project ID (default: gcloud's current project)
#   REGION       Cloud Run region (default: us-central1). Pick one near your Supabase region.
#   ANTHROPIC_API_KEY, OPENAI_API_KEY, SMTP_PASSWORD, GOOGLE_CLIENT_SECRET,
#   CANVAS_OAUTH_CLIENTS  Stored in Secret Manager when given; kept from earlier runs if not.
#   DATABASE_URL Needed on the first run; kept from earlier runs if not given.
#
# Non-secret settings come from deploy/cloudrun.env.yaml.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="defog-api"
REPO="defog"
SA_NAME="defog-api"
ENV_FILE="deploy/cloudrun.env.yaml"

if [[ -z "$PROJECT" ]]; then
  echo "Set PROJECT=<your project id> or run: gcloud config set project <id>" >&2
  exit 1
fi
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/api:$(date +%Y%m%d-%H%M%S)"
APP_URL="$(sed -n 's/^APP_URL: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$ENV_FILE")"
if [[ "$APP_URL" != https://* ]]; then
  echo "Set APP_URL in $ENV_FILE to the web app's https:// address." >&2
  exit 1
fi

step() { printf '\n==> %s\n' "$*"; }
random_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
fernet_key() { python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'; }
secret_name() { echo "defog-$(echo "$1" | tr 'A-Z_' 'a-z-')"; }
secret_exists() { gcloud secrets describe "$(secret_name "$1")" --project "$PROJECT" >/dev/null 2>&1; }

# Store $2 as the newest version of secret $1 (creating it if needed).
put_secret() {
  local name; name="$(secret_name "$1")"
  if ! secret_exists "$1"; then
    gcloud secrets create "$name" --project "$PROJECT" --replication-policy automatic --quiet >/dev/null
  fi
  printf '%s' "$2" | gcloud secrets versions add "$name" --project "$PROJECT" --data-file=- --quiet >/dev/null
  echo "    stored $1"
}

step "Project $PROJECT, region $REGION"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com --project "$PROJECT"

step "Service account"
if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "Defog API"
fi

step "Secrets (Secret Manager)"
# Required. Keys are generated once and then kept: changing TOKEN_ENCRYPTION_KEY would make
# every stored Canvas/Google token unreadable.
if [[ -n "${DATABASE_URL:-}" ]]; then
  put_secret DATABASE_URL "$DATABASE_URL"
elif ! secret_exists DATABASE_URL; then
  echo "DATABASE_URL is required on the first run (Supabase -> Connect -> Session pooler)." >&2
  exit 1
fi
secret_exists SECRET_KEY || put_secret SECRET_KEY "$(random_secret)"
secret_exists TOKEN_ENCRYPTION_KEY || put_secret TOKEN_ENCRYPTION_KEY "$(fernet_key)"
secret_exists SYNC_TRIGGER_SECRET || put_secret SYNC_TRIGGER_SECRET "$(random_secret)"
# Optional: store when given, keep when stored earlier.
for name in ANTHROPIC_API_KEY OPENAI_API_KEY SMTP_PASSWORD GOOGLE_CLIENT_SECRET CANVAS_OAUTH_CLIENTS; do
  if [[ -n "${!name:-}" ]]; then put_secret "$name" "${!name}"; fi
done

SECRET_FLAGS=()
for name in DATABASE_URL SECRET_KEY TOKEN_ENCRYPTION_KEY SYNC_TRIGGER_SECRET \
  ANTHROPIC_API_KEY OPENAI_API_KEY SMTP_PASSWORD GOOGLE_CLIENT_SECRET CANVAS_OAUTH_CLIENTS; do
  if secret_exists "$name"; then
    gcloud secrets add-iam-policy-binding "$(secret_name "$name")" --project "$PROJECT" \
      --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor --quiet >/dev/null
    SECRET_FLAGS+=("${name}=$(secret_name "$name"):latest")
  fi
done
SECRETS="$(IFS=,; echo "${SECRET_FLAGS[*]}")"

step "Build the image (Cloud Build)"
if ! gcloud artifacts repositories describe "$REPO" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO" --project "$PROJECT" --location "$REGION" \
    --repository-format docker --description "Defog images"
fi
gcloud builds submit . --project "$PROJECT" --region "$REGION" --tag "$IMAGE"

step "Database migrations (Cloud Run job)"
gcloud run jobs deploy defog-migrate --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA" --set-secrets "DATABASE_URL=$(secret_name DATABASE_URL):latest" \
  --command alembic --args upgrade,head --max-retries 0 --task-timeout 10m --quiet
gcloud run jobs execute defog-migrate --project "$PROJECT" --region "$REGION" --wait

step "Deploy the API (Cloud Run service)"
ENV_TMP="$(mktemp)"
trap 'rm -f "$ENV_TMP"' EXIT
{
  grep -vE '^\s*(#|$)' "$ENV_FILE" | grep -vE '^(ENVIRONMENT|API_URL|BACKGROUND_SYNC_ENABLED):'
  echo 'ENVIRONMENT: "production"'
  echo "API_URL: \"$APP_URL\""
  # Cloud Run pauses idle servers, so Cloud Scheduler runs the sync instead (below).
  echo 'BACKGROUND_SYNC_ENABLED: "false"'
} > "$ENV_TMP"
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA" --allow-unauthenticated \
  --env-vars-file "$ENV_TMP" --set-secrets "$SECRETS" \
  --cpu 1 --memory 512Mi --min-instances 0 --max-instances 2 --timeout 3600 --quiet
SERVICE_URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"

step "Scheduled sync (Cloud Scheduler, every 15 minutes)"
SYNC_SECRET="$(gcloud secrets versions access latest --secret "$(secret_name SYNC_TRIGGER_SECRET)" --project "$PROJECT")"
JOB_ARGS=(--project "$PROJECT" --location "$REGION" --schedule "*/15 * * * *"
  --uri "${SERVICE_URL}/api/internal/sync" --http-method POST --attempt-deadline 30m)
if gcloud scheduler jobs describe defog-sync --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http defog-sync "${JOB_ARGS[@]}" --update-headers "X-Sync-Secret=${SYNC_SECRET}" --quiet >/dev/null
else
  gcloud scheduler jobs create http defog-sync "${JOB_ARGS[@]}" --headers "X-Sync-Secret=${SYNC_SECRET}" --quiet >/dev/null
fi

step "Check"
curl -fsS "${SERVICE_URL}/api/health" && echo

cat <<EOF

Done. The API is running at:

  ${SERVICE_URL}

Next: point the Cloudflare Worker at it. In frontend/wrangler.jsonc set
  "BACKEND_URL": "${SERVICE_URL}"
and run: npm run deploy
EOF
