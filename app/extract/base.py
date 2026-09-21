"""抽出器の共通インターフェース。GEMINI_API_KEY があれば Gemini、なければモック。"""
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


def get_extractor() -> Extractor:
    if settings.use_gemini:
        from .gemini import GeminiExtractor
        return GeminiExtractor(api_key=settings.gemini_api_key, model=settings.gemini_model)
    from .mock import MockExtractor
    return MockExtractor()
