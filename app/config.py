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
    store_backend: str = os.getenv("STORE_BACKEND", "memory")
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    firestore_prefix: str = os.getenv("FIRESTORE_COLLECTION_PREFIX", "ocr_trust")
    gcs_bucket: str = os.getenv("GCS_BUCKET", "")
    policy_path: Path = ROOT / os.getenv("POLICY_PATH", "app/trust/policy.yaml")
    model_dir: Path = ROOT / os.getenv("MODEL_DIR", "models")
    master_dir: Path = ROOT / "data" / "master"

    @property
    def use_gemini(self) -> bool:
        return bool(self.gemini_api_key)


settings = Settings()
