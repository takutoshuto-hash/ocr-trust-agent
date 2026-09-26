"""行動するエージェント（Resolver）。

ジャッジで FAIL / 二重読み取り不一致になった項目に対し、エージェントが行動を選んで候補値を作り、
決定的ルールで採否を決める。人に回すのは最後の手段。

  行動の選択:  ルールプランナー（オフライン）／ADK プランナー（Gemini あり・ポリシーで許可）
  行動の実行:  actions.py の決定的関数（切り出し再読み取り、高精度再読み取り、郵便番号補完、マスタ近似）
  採否の判定:  ここ（決定的）。候補が再検証に合格し、かつ「独立した2つの読み」が一致したときだけ採用
  ガバナンス:  1帳票あたりの行動回数・高精度モデルの可否は policy.yaml。全行動を監査ログへ
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from app.config import settings
from app.schemas import CheckStatus, Extraction, FieldVerdict, field_type_of
from . import actions as A
from .actions import Candidate, FieldContext

ACTIONS = ["reread_zone", "reread_premium", "complete_address_from_zip", "nearest_product_code", "escalate_to_human"]


class Resolver:
    def __init__(self, extractor, judge, policy: dict, planner: str = "rules"):
        self.extractor = extractor
        self.judge = judge
        self.cfg = policy or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        self.max_per_form = int(self.cfg.get("max_per_form", 6))
        self.max_rereads_per_field = int(self.cfg.get("max_rereads_per_field", 1))
        self.allow_premium = bool(self.cfg.get("allow_premium", False))
        self.planner = planner if (planner != "adk" or settings.use_gemini) else "rules"

    # ------------------------------------------------------------------ 入口
    def resolve(self, primary: Extraction, secondary: Optional[Extraction], verdicts: dict[str, FieldVerdict], *,
                image: Optional[bytes], format_id: str, hint=None, history: Optional[list] = None) -> tuple[dict[str, str], list[dict]]:
        """戻り値: ({path: 採用した新しい値}, 行動ログ)。verdicts は採用した項目について更新される。"""
        if not self.enabled:
            return {}, []
        from app.judge.core import _history_index, _past_values
        flat = primary.form.flatten()
        flat2 = secondary.form.flatten() if secondary else {}
        secondary_source = "zones" if (secondary and str(secondary.model).endswith(":zones")) else "page"
        past = _history_index(history or [])
        updates: dict[str, str] = {}
        log: list[dict] = []
        budget = self.max_per_form

        targets = [p for p, v in verdicts.items() if (v.any_fail or v.agreement is False) and not isinstance(flat[p], int)]
        for path in targets:
            if budget <= 0:
                log.append({"path": path, "action": "escalate_to_human", "reason": "帳票あたりの行動上限に到達"})
                continue
            v = verdicts[path]
            prefix = path.rsplit(".", 1)[0]
            ctx = FieldContext(
                path=path, field_type=v.field_type, value=str(flat[path]),
                evidence=(primary.fields.get(path).evidence if primary.fields.get(path) else ""),
                secondary_value=(str(flat2[path]) if path in flat2 else None),
                reasons=[c.detail or c.name for c in v.checks if c.status == CheckStatus.FAIL] + (["二重読み取り不一致"] if v.agreement is False else []),
                sibling={k.rsplit(".", 1)[1]: str(flat.get(f"{prefix}.{k.rsplit('.', 1)[1]}", "")) for k in flat if k.startswith(prefix + ".")},
                image=image, format_id=format_id, secondary_source=secondary_source,
                history_values=[str(x) for x in _past_values(past, path, v.field_type, flat)] if history else [],
            )
            plan = self._plan(ctx)
            accepted: Optional[Candidate] = None
            rereads = 0
            hint_value = _hint_value(hint, path)
            for action in plan:
                if budget <= 0 or action == "escalate_to_human":
                    break
                if action in ("reread_zone", "reread_premium"):
                    if rereads >= self.max_rereads_per_field:
                        continue
                    if action == "reread_premium" and not self.allow_premium:
                        log.append({"path": path, "action": action, "skipped": "ポリシーで高精度モデル不許可"})
                        continue
                    rereads += 1
                budget -= 1
                try:
                    cand = self._execute(action, ctx, hint_value)
                    err = None
                except Exception as e:          # 混雑（429）など。修復は諦めて人に回す（受付そのものは止めない）
                    cand, err = None, f"{type(e).__name__}: {str(e)[:160]}"
                entry = {"path": path, "action": action, "candidate": (cand.value if cand else None), "detail": (cand.detail if cand else "候補なし")}
                if err:
                    entry["error"] = err
                if cand is not None:
                    ok, why = self._accept(ctx, cand, flat)
                    entry["accepted"], entry["why"] = ok, why
                    if ok:
                        accepted = cand
                log.append(entry)
                if accepted:
                    break
            if accepted:
                updates[path] = accepted.value
                flat[path] = accepted.value
            else:
                log.append({"path": path, "action": "escalate_to_human", "reason": "採用できる候補なし"})
        return updates, log

    # ------------------------------------------------------------------ 計画
    def _plan(self, ctx: FieldContext) -> list[str]:
        if self.planner == "adk":
            try:
                from app.judge._async import run_coro
                return run_coro(_plan_with_adk(ctx, self.allow_premium))
            except Exception as e:      # ADK 失敗時はルールに退避（監査に残す）
                ctx.log.append({"planner": "adk", "error": str(e)[:200]})
        return _plan_with_rules(ctx, self.allow_premium)

    # ------------------------------------------------------------------ 実行
    def _execute(self, action: str, ctx: FieldContext, hint_value) -> Optional[Candidate]:
        if action == "reread_zone":
            return A.act_reread_zone(ctx, self.extractor, premium=False, hint=hint_value)
        if action == "reread_premium":
            return A.act_reread_zone(ctx, self.extractor, premium=True, hint=hint_value)
        if action == "complete_address_from_zip":
            return A.act_complete_address_from_zip(ctx)
        if action == "restore_from_history":
            return A.act_restore_from_history(ctx)
        if action == "nearest_product_code":
            return A.act_nearest_product_code(ctx)
        return None

    # ------------------------------------------------------------------ 採否（決定的）
    def _accept(self, ctx: FieldContext, cand: Candidate, flat: dict) -> tuple[bool, str]:
        """候補を採用する条件（両方必要）:
        1. 候補値で再検証して FAIL が無い
        2. 独立した2つの読みが一致する: 候補 == 二重読み取りの値、または 候補 == 元の値（外部知識由来の補完は元の値の"訂正"なので、
           候補が郵便番号マスタ等の決定的根拠を持つ場合は 2 を満たしたとみなす）
        """
        if not cand.value:
            return False, "候補が空"
        trial = dict(flat); trial[ctx.path] = cand.value
        checks = self.judge._checks_for(ctx.field_type, ctx.path, cand.value, trial)
        fails = [c for c in checks if c.status == CheckStatus.FAIL]
        if fails:
            return False, "再検証 FAIL: " + "; ".join(c.detail or c.name for c in fails)
        norm = _norm
        if cand.source in ("complete_address_from_zip", "nearest_product_code"):
            return True, "決定的根拠（マスタ）＋再検証合格"
        if cand.source == "restore_from_history":
            return True, "決定的根拠（人が確定した字体）＋再検証合格"
        agree_secondary = ctx.secondary_value is not None and norm(cand.value) == norm(ctx.secondary_value)
        agree_primary = norm(cand.value) == norm(ctx.value)
        if cand.source in ("reread_zone", "reread_premium") and ctx.secondary_source == "zones":
            # 再読み取りは 2 回目と同じ入力（欄切り出し）なので、2 回目との一致は独立した根拠にならない。
            # 1 回目（全面読み）と一致したときだけ採用する（欄切り出し同士の一致で誤りを通してしまった実測への対処）
            if agree_primary:
                return True, "独立した読み（1回目・全面）と一致＋再検証合格"
            return False, "欄切り出し同士の一致は独立でない（人が確認）"
        if agree_secondary or agree_primary:
            return True, "独立した読みと一致（" + ("二重読み取り" if agree_secondary else "元の読み") + "）＋再検証合格"
        return False, "独立した読みと不一致（人が確認）"


# ---------------------------------------------------------------------- プランナー
def _plan_with_rules(ctx: FieldContext, allow_premium: bool) -> list[str]:
    ft = ctx.field_type
    plan: list[str] = []
    if ctx.history_values:
        plan.append("restore_from_history")     # 決定的・無料: 過去の確定値と異体字だけ違うなら字体を戻す
    if ft.endswith(".product_code"):
        plan += ["nearest_product_code", "reread_zone"]
    elif ft.endswith(".address"):
        plan += ["complete_address_from_zip", "reread_zone"]
    elif ft.endswith(".zip"):
        plan += ["reread_zone"]
    else:
        plan += ["reread_zone"]
    if allow_premium:
        plan.append("reread_premium")
    plan.append("escalate_to_human")
    return plan


_ADK_INSTRUCTION = (
    "あなたは手書き注文書の読み取り結果を修復する担当者です。与えられた項目の状況（値・根拠・検証の失敗理由・二重読み取りの値・"
    "同じブロックの他項目）を見て、取るべき行動を順番に選び、choose_actions ツールで返してください。"
    "使える行動: reread_zone（欄だけ切り出して再読み取り）, reread_premium（高精度モデルで再読み取り。許可されている場合のみ）, "
    "complete_address_from_zip（郵便番号から住所の先頭を補完。住所または郵便番号の項目向け）, nearest_product_code（商品コードのマスタ近似一致）, "
    "restore_from_history（過去に人が確定した値と異体字だけが違うとき、その字体に戻す。過去の確定値がある場合のみ）, "
    "escalate_to_human（人に回す。最後に必ず含める）。安い決定的な行動を先に、再読み取りは1回まで。値を自分で創作しないこと。"
)


async def _plan_with_adk(ctx: FieldContext, allow_premium: bool) -> list[str]:
    from google.adk.agents import Agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    chosen: dict[str, list[str]] = {}

    def choose_actions(actions: list[str]) -> dict:
        """行動の順番を確定する。actions は許可された行動名のリスト。"""
        allowed = [a for a in actions if a in ACTIONS and (a != "reread_premium" or allow_premium)]
        if "escalate_to_human" not in allowed:
            allowed.append("escalate_to_human")
        chosen["plan"] = allowed
        return {"ok": True, "plan": allowed}

    from app.config import make_adk_model, make_adk_config
    agent = Agent(name="ocr_resolver", model=make_adk_model(), instruction=_ADK_INSTRUCTION, tools=[choose_actions], **({"generate_content_config": make_adk_config()} if make_adk_config() else {}))
    runner = InMemoryRunner(agent=agent, app_name="ocr_trust")
    session = await runner.session_service.create_session(app_name="ocr_trust", user_id="resolver")
    text = (f"項目: {ctx.path}（種別 {ctx.field_type}）\n値: {ctx.value!r}\n根拠: {ctx.evidence!r}\n"
            f"二重読み取り: {ctx.secondary_value!r}\n検証の失敗: {ctx.reasons}\n同ブロック: {ctx.sibling}\n"
            f"過去の確定値: {ctx.history_values[:3]!r}\n"
            f"高精度モデル: {'許可' if allow_premium else '不許可'}")
    msg = types.Content(role="user", parts=[types.Part(text=text)])
    from app.extract.usage import GLOBAL as USAGE
    async for ev in runner.run_async(user_id="resolver", session_id=session.id, new_message=msg):
        USAGE.add(settings.gemini_model, getattr(ev, "usage_metadata", None))
    return chosen.get("plan") or _plan_with_rules(ctx, allow_premium)


def _hint_value(hint, path: str):
    if hint is None:
        return None
    return hint.flatten().get(path)


def _norm(x) -> str:
    return str(x if x is not None else "").replace(" ", "").replace("　", "").replace("-", "").upper()
