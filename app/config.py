"""環境変数から設定を読む。.env があれば読み込む（python-dotenv 不要の簡易版）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    # Vertex AI 経由（本番推奨）: 上限が AI Studio キーと別枠で、Google Cloud のクレジットで課金される。
    # Cloud Run では ADC（サービスアカウント）で認証。ローカルは GOOGLE_OAUTH_ACCESS_TOKEN でも可
    use_vertex: bool = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes")
    vertex_location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    store_backend: str = os.getenv("STORE_BACKEND", "memory")
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    firestore_prefix: str = os.getenv("FIRESTORE_COLLECTION_PREFIX", "ocr_trust")
    gcs_bucket: str = os.getenv("GCS_BUCKET", "")
    policy_path: Path = ROOT / os.getenv("POLICY_PATH", "app/trust/policy.yaml")
    model_dir: Path = ROOT / os.getenv("MODEL_DIR", "models")
    master_dir: Path = ROOT / "data" / "master"

    @property
    def use_gemini(self) -> bool:
        return bool(self.gemini_api_key) or self.use_vertex


def make_adk_model():
    """ADK の LlmAgent に渡すモデル。抽出器と同じ genai クライアント（Vertex / AI Studio、ローカルの gcloud トークン更新）を共有する。
    ADK 既定のクライアントは ADC しか見ないため、ローカルの長時間実行では ADC が無いと ADK だけ黙って失敗していた。"""
    from google.adk.models import Gemini
    return Gemini(model=settings.gemini_model, client=make_genai_client())


def make_genai_client():
    """google-genai クライアント。Vertex AI（ADC or アクセストークン）か AI Studio キー。"""
    from google import genai
    if settings.use_vertex:
        creds = None
        if os.getenv("GOOGLE_GENAI_USE_GCLOUD_TOKEN", "").lower() in ("1", "true", "yes"):
            creds = GcloudCliCredentials()          # ローカルの長時間実行用: gcloud のトークンを失効前に自動更新
        elif os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN"):
            from google.oauth2.credentials import Credentials
            creds = Credentials(token=os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"])
        return genai.Client(vertexai=True, project=settings.gcp_project or None, location=settings.vertex_location, credentials=creds)
    return genai.Client(api_key=settings.gemini_api_key)


def GcloudCliCredentials():
    """`gcloud auth print-access-token` を refresh() で呼ぶ資格情報（ADC 未設定のローカル用）。"""
    import datetime
    import subprocess
    from google.auth import credentials as ga_credentials

    class _Creds(ga_credentials.Credentials):
        def refresh(self, request):
            exe = "gcloud.cmd" if os.name == "nt" else "gcloud"
            token = subprocess.run([exe, "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
            self.token = token
            # gcloud は失効までキャッシュした同じトークンを返すので、短い周期で問い合わせ直す（gcloud 側が失効前に更新する）
            self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(minutes=5)

    c = _Creds()
    c.refresh(None)
    return c


settings = Settings()
