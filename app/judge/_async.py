"""同期コードから ADK（async）を安全に呼ぶ。

FastAPI の async エンドポイントの中（＝イベントループ実行中）から asyncio.run() を呼ぶと失敗するため、
ループが動いていれば別スレッドで新しいループを作って実行する。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Coroutine


def run_coro(coro: Coroutine[Any, Any, Any], timeout: float = 120.0) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)                     # ループ無し（CLI・テスト・同期エンドポイント）
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result(timeout=timeout)
