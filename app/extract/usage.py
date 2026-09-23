"""Gemini のトークン使用量と費用の実測記録（推定ではなく usage_metadata の実数から計算）。

- 抽出器（全面読み・欄切り出し読み・欄再読み取り）と ADK エージェント（行動計画・振り返り・説明）の呼び出しをすべて集計
- 単価は Vertex AI の公開価格（USD / 100万トークン）。思考トークンは出力単価で課金される
- 為替は JPY_PER_USD（既定 150）
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict

# (input, output) USD per 1M tokens. 出力には思考トークンを含む
PRICES_USD_PER_M = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}


class Usage:
    def __init__(self):
        self._lock = threading.Lock()
        self._m: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "prompt": 0, "output": 0, "thoughts": 0})

    def add(self, model: str, um, *, kind: str = "") -> None:
        """um: google.genai の usage_metadata（無ければ何もしない）。"""
        if um is None:
            return
        key = _norm_model(model)
        with self._lock:
            c = self._m[key]
            c["calls"] += 1
            c["prompt"] += int(getattr(um, "prompt_token_count", 0) or 0)
            c["output"] += int(getattr(um, "candidates_token_count", 0) or 0)
            c["thoughts"] += int(getattr(um, "thoughts_token_count", 0) or 0)

    def snapshot(self) -> dict:
        with self._lock:
            models = {k: dict(v) for k, v in self._m.items()}
        rate = float(os.getenv("JPY_PER_USD", "150"))
        total_usd = 0.0
        for k, v in models.items():
            pin, pout = PRICES_USD_PER_M.get(k, (0.30, 2.50))
            v["cost_usd"] = round(v["prompt"] / 1e6 * pin + (v["output"] + v["thoughts"]) / 1e6 * pout, 5)
            total_usd += v["cost_usd"]
        return {"models": models, "calls": sum(v["calls"] for v in models.values()),
                "prompt": sum(v["prompt"] for v in models.values()), "output": sum(v["output"] for v in models.values()),
                "thoughts": sum(v["thoughts"] for v in models.values()),
                "cost_usd": round(total_usd, 4), "cost_jpy": round(total_usd * rate, 1), "jpy_per_usd": rate}


def _norm_model(model: str) -> str:
    m = (model or "").split(":")[0]
    m = m.split("/")[-1]
    for k in PRICES_USD_PER_M:
        if m.startswith(k):
            return k
    return m or "unknown"


GLOBAL = Usage()   # プロセス全体の累計（Cloud Run は起動後、シミュレーションは実行開始後）


def diff(before: dict, after: dict) -> dict:
    """snapshot の差分（1 日分・1 帳票分の費用を出す）。"""
    return {k: round(after[k] - before[k], 4) if isinstance(after.get(k), (int, float)) else after.get(k)
            for k in ("calls", "prompt", "output", "thoughts", "cost_usd", "cost_jpy")}
