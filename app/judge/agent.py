"""ADK ジャッジエージェント。

決定的ツール（tools.py）を FunctionTool として持ち、要確認項目について
「なぜ要確認か・人は何を見ればよいか」を自然文で説明する。
判定そのものは core.Judge が行うので、LLM は説明係に限定する（ガバナンス: 判断は再現可能に）。

使い方: explain_review(form_decision) -> str
GEMINI_API_KEY が無い環境では固定文を返す。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.config import settings
from app.schemas import FieldStatus, FormDecision
from . import tools as T

_INSTRUCTION = (
    "あなたは手書き注文書の読み取り結果を検証する担当者です。"
    "与えられた要確認項目の一覧（検証結果・二重読み取りの一致）をもとに、確認者が最短で確認できるよう、"
    "項目ごとに『何が疑わしいか』『原本のどこを見るべきか』を日本語で簡潔に箇条書きにしてください。"
    "必要なら郵便番号・商品コード・電話番号の検証ツールを呼んで根拠を補強してください。値を勝手に修正しないこと。"
)


def _build_agent():
    from google.adk.agents import Agent
    return Agent(
        name="ocr_judge",
        model=settings.gemini_model,
        description="OCR結果の要確認項目を説明する検証エージェント",
        instruction=_INSTRUCTION,
        tools=[T.check_zip_address, T.check_product_code, T.check_phone_format, T.check_kana],
    )


def _summary_text(fd: FormDecision) -> str:
    lines = []
    for path, d in fd.decisions.items():
        if not d.human_sees:
            continue
        v = fd.verdicts[path]
        lines.append(f"- {path} = {d.value!r} / 理由: {'; '.join(d.reasons)} / 検証: {v.reason}")
    return "\n".join(lines) or "（要確認項目なし）"


def explain_review(fd: FormDecision) -> str:
    if not fd.needs_review:
        return "要確認項目はありません。"
    if not settings.use_gemini:
        return "【オフライン】\n" + _summary_text(fd)
    return asyncio.run(_explain_async(fd))


async def _explain_async(fd: FormDecision) -> str:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(agent=_build_agent(), app_name="ocr_trust")
    session = await runner.session_service.create_session(app_name="ocr_trust", user_id="reviewer")
    msg = types.Content(role="user", parts=[types.Part(text="要確認項目:\n" + _summary_text(fd))])
    out: Optional[str] = None
    async for ev in runner.run_async(user_id="reviewer", session_id=session.id, new_message=msg):
        if ev.is_final_response() and ev.content and ev.content.parts:
            out = "".join(p.text or "" for p in ev.content.parts)
    return out or _summary_text(fd)
