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
  --set-env-vars "STORE_BACKEND=${STORE_BACKEND:-firestore},GOOGLE_CLOUD_PROJECT=${PROJECT},GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.5-flash},GOOGLE_GENAI_USE_VERTEXAI=${USE_VERTEX:-true},GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION:-global}" \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,REVIEW_USER=REVIEW_USER:latest,REVIEW_PASSWORD=REVIEW_PASSWORD:latest" \
  --memory 1Gi --cpu 1 --min-instances "${MIN_INSTANCES:-0}" --max-instances 5   # 提出〜審査（12/1）は MIN_INSTANCES=1 で起動待ちを無くす

echo "deployed: $(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
