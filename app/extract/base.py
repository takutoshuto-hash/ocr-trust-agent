"""抽出器の共通インターフェースと、プロファイルに応じた選択。

  profile=secure       → Gemma（Ollama 互換 API、画像を外に出さない）
  GEMINI_API_KEY あり   → Gemini
  それ以外              → モック（合成データの正解に誤りを混入。オフライン評価用）
"""
from __future__ import annotations

from typing import Optional, Protocol

from app.config import settings
from app.schemas import Extraction, OrderForm


class Extractor(Protocol):
    name: str

    def extract(
        self,
        image: bytes,
        *,
        mime_type: str = "image/png",
        examples: Optional[list[tuple[str, OrderForm]]] = None,   # (説明, 過去の確定値) few-shot
        variant: int = 0,                                         # 二重読み取り用: 0=通常, 1=別プロンプト
        hint: Optional[OrderForm] = None,                         # モック専用: 正解（合成データの sidecar）
    ) -> Extraction: ...


def get_extractor(profile: str = "lean") -> Extractor:
    if profile == "secure":
        from .gemma_local import GemmaLocalExtractor
        return GemmaLocalExtractor()
    if settings.use_gemini:
        from .gemini import GeminiExtractor
        return GeminiExtractor(api_key=settings.gemini_api_key, model=settings.gemini_model)
    from .mock import MockExtractor
    return MockExtractor()


def sends_to_cloud(extractor: Extractor) -> bool:
    return extractor.name == "gemini"
