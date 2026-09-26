"""テストは常にモック抽出器で回す（本番の Gemini を呼ばない: 費用・ネットワーク・偽画像の 400 を避ける）。
ターミナルに GEMINI_API_KEY や Vertex の環境変数が残っていても、テストの中では外す。"""
import os

for k in ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCLOUD_TOKEN"):
    os.environ.pop(k, None)
