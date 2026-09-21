#!/usr/bin/env bash
# Cloud Run へデプロイ。事前に: gcloud auth login && gcloud config set project <PROJECT>
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project)}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-ocr-trust-agent}"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com firestore.googleapis.com \
  artifactregistry.googleapis.com --project "$PROJECT"

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "STORE_BACKEND=${STORE_BACKEND:-firestore},GOOGLE_CLOUD_PROJECT=${PROJECT},GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.5-flash}" \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest" \
  --memory 1Gi --cpu 1 --min-instances 0 --max-instances 5

echo "deployed: $(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
