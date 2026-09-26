"""同期コードから ADK（async）を安全に呼ぶ。

- FastAPI の async エンドポイントの中（＝イベントループ実行中）から asyncio.run() を呼ぶと失敗する。
- asyncio.run() を呼ぶたびにループを作って閉じると、共有している genai の非同期クライアント（httpx の接続）が
  閉じたループに縛られ、次の呼び出しで「Event loop is closed」の警告や失敗が出る（実手書き評価で観測）。
そこで、プロセスに 1 本だけ常駐のループ（別スレッド）を持ち、すべての ADK 呼び出しをそこで実行する。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, name="ocr-trust-adk-loop", daemon=True)
            t.start()
            _loop = loop
        return _loop


def run_coro(coro: Coroutine[Any, Any, Any], timeout: float = 120.0) -> Any:
    """コルーチンを常駐ループで実行して結果を返す（呼び出し側がループの中でも外でも同じ）。"""
    fut = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    try:
        return fut.result(timeout=timeout)
    except BaseException:
        fut.cancel()
        raise
