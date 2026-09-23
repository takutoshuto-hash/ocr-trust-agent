"""振り返りエージェント: 集計を読んで「読み取りルール」「ポリシー値の変更」を提案する。人が承認するまで何も変えない。

  提案の生成: ADK エージェント（Gemini あり）／ルール（オフライン）
  ガバナンス: ポリシー変更は ALLOWED_POLICY_KEYS の範囲内だけ。範囲外の提案は自動で却下（監査に残す）
  適用:       承認 → ルールは store.rules に保存され抽出プロンプトへ注入 / ポリシーは store の上書きとして保存
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from app.config import settings
from app.schemas import ApprovedRule, Proposal, ProposalKind

# 振り返りエージェントが提案してよいポリシーキーと範囲（人が宣言するガバナンス）
ALLOWED_POLICY_KEYS: dict[str, tuple[float, float]] = {
    "hallucination.blank_ink_ratio": (0.003, 0.03),
    "audit_sampling_rate": (0.01, 0.2),
    "promotion.L1.min_streak": (20, 200),
    "promotion.L1.min_samples": (30, 500),
    "router.target_error_rate": (0.001, 0.02),
    "min_samples_for_sender": (3, 30),
}


class ReflectionAgent:
    def __init__(self, store, policy_raw: dict, planner: str = "rules"):
        self.store = store
        self.policy = policy_raw
        self.planner = planner if (planner != "adk" or settings.use_gemini) else "rules"

    # ------------------------------------------------------------------ 提案
    MAX_ACTIVE_RULES_PER_FIELD = 2

    def propose(self, analysis: dict) -> list[Proposal]:
        if analysis.get("window_records", 0) == 0:
            return []
        raw: list[dict]
        proposer = "reflection_rules"
        self._rates = {row["key"]: row["rate"] for row in analysis.get("by_field_type", [])}
        self._active_rules = self.store.list_rules()
        analysis = dict(analysis, active_rules=[{"rule_id": r.rule_id, "field_type": r.field_type, "baseline_rate": r.baseline_rate,
                                                 "text": r.text[:80]} for r in self._active_rules])
        if self.planner == "adk":
            try:
                from app.judge._async import run_coro
                raw = run_coro(_propose_with_adk(analysis, self.policy), timeout=300)
                proposer = "reflection_agent(adk)"
            except Exception as e:
                raw = _propose_with_rules(analysis, self.policy)
                raw.append({"kind": "note", "title": "ADK 失敗によりルール提案へ退避", "rationale": str(e)[:200]})
        else:
            raw = _propose_with_rules(analysis, self.policy)

        raw = raw + self._retractions(analysis)          # 自分が出したルールの効果を測り、効かなかったものは取り消しを提案
        out: list[Proposal] = []
        existing = {(p.kind, p.rule_text, p.policy_key) for p in self.store.list_proposals(status="pending")}
        for d in raw:
            p = self._to_proposal(d, proposer)
            if p is None:
                continue
            if (p.kind, p.rule_text, p.policy_key) in existing:
                continue   # 同じ提案が未処理で残っていれば重複させない
            self.store.put_proposal(p)
            out.append(p)
        return out

    def _to_proposal(self, d: dict, proposer: str) -> Optional[Proposal]:
        kind = d.get("kind")
        base = dict(proposal_id=uuid.uuid4().hex[:10], title=str(d.get("title", ""))[:120],
                    rationale=str(d.get("rationale", ""))[:800], evidence=d.get("evidence", {}) or {}, proposer=proposer)
        if kind == "retract" and d.get("rule_id"):
            return Proposal(kind=ProposalKind.RETRACT, retract_rule_id=str(d["rule_id"]), **base)
        if kind == "rule" and d.get("rule_text"):
            scope = str(d.get("scope") or "global")
            if not (scope == "global" or (scope.startswith("format:") and "." not in scope)):
                scope = "global"        # LLM が項目名などをスコープに入れてきたら global に正規化（注入されないルールを作らない）
            ft, a, b = _confusion_of(d)
            ev = dict(base["evidence"]); ev.update({"field_type": ft, "from": a, "to": b, "baseline_rate": getattr(self, "_rates", {}).get(ft)})
            base["evidence"] = ev

            def rejected_rule(reason: str):
                kw = {**base, "title": "（自動却下）" + base["title"], "rationale": reason + " / 元の根拠: " + base["rationale"]}
                return Proposal(kind=ProposalKind.RULE, rule_text=str(d["rule_text"])[:300], rule_scope=scope, status="rejected",
                                decided_by="governance", **kw)

            from app.judge.tools import VARIANT_KANJI
            if a and b and VARIANT_KANJI.get(a, a) == VARIANT_KANJI.get(b, b):
                return rejected_rule("異体字（髙↔高 など）はルールで扱わない。根拠からの復元と顧客照合で対処する")
            active = self.store.list_rules()     # 直前の承認も見る（同じ夜に同じ混同を2件通さない）
            if a and b and any(r.field_type == ft and r.confusion in (f"{a}>{b}", f"{b}>{a}") for r in active):
                return rejected_rule("同じ混同（または逆向き）のルールが既に有効。矛盾するルールを積まない")
            if ft and sum(1 for r in active if r.field_type == ft) >= self.MAX_ACTIVE_RULES_PER_FIELD:
                return rejected_rule(f"この項目種別の有効ルールが上限（{self.MAX_ACTIVE_RULES_PER_FIELD}）。効かないルールの取り消しが先")
            return Proposal(kind=ProposalKind.RULE, rule_text=str(d["rule_text"])[:300], rule_scope=scope, **base)
        if kind == "policy":
            key, to = d.get("policy_key"), d.get("policy_to")
            def rejected(reason: str, to_val):
                kw = {**base, "title": "（自動却下）" + base["title"], "rationale": reason + " / 元の根拠: " + base["rationale"]}
                return Proposal(kind=ProposalKind.POLICY, policy_key=str(key), policy_to=to_val, status="rejected",
                                decided_by="governance", **kw)

            if key not in ALLOWED_POLICY_KEYS:
                return rejected(f"許可されていないキー: {key}", to)
            lo, hi = ALLOWED_POLICY_KEYS[key]
            try:
                to_f = float(to)
            except (TypeError, ValueError):
                return None
            if not (lo <= to_f <= hi):
                return rejected(f"範囲外 [{lo}, {hi}]: {to_f}", to_f)
            return Proposal(kind=ProposalKind.POLICY, policy_key=key, policy_from=_get_path(self.policy, key), policy_to=to_f, **base)
        return None

    # ------------------------------------------------------------------ 承認・却下
    def decide(self, proposal_id: str, approve: bool, actor: str) -> Proposal:
        from app.schemas import now_utc
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status != "pending":
            return p
        p.status = "approved" if approve else "rejected"
        p.decided_by, p.decided_at = actor, now_utc()
        self.store.put_proposal(p)
        if approve:
            if p.kind == ProposalKind.RULE and p.rule_text:
                ev = p.evidence or {}
                br = ev.get("baseline_rate")
                self.store.put_rule(ApprovedRule(rule_id=uuid.uuid4().hex[:10], text=p.rule_text, scope=p.rule_scope, source_proposal=p.proposal_id,
                                                 field_type=str(ev.get("field_type") or ""),
                                                 confusion=(f"{ev.get('from')}>{ev.get('to')}" if ev.get("from") and ev.get("to") else ""),
                                                 baseline_rate=(float(br) if br is not None else None)))
            elif p.kind == ProposalKind.RETRACT and p.retract_rule_id:
                self.store.deactivate_rule(p.retract_rule_id)
            elif p.kind == ProposalKind.POLICY and p.policy_key:
                self.store.put_policy_override(p.policy_key, p.policy_to)
                _set_path(self.policy, p.policy_key, p.policy_to)   # 実行中のポリシーにも反映
        return p


    # ------------------------------------------------------------------ 効果測定
    def _retractions(self, analysis: dict) -> list[dict]:
        """有効ルールのうち、提案時より対象項目の修正率が下がっていないものは取り消しを提案する。

        評価できるのは「集計窓の開始より前に承認された」ルール（窓の全期間で効いていたもの）だけ。
        """
        since = analysis.get("since")
        rates = getattr(self, "_rates", {})
        out: list[dict] = []
        for r in getattr(self, "_active_rules", []) or []:
            if not r.field_type or r.baseline_rate is None or r.field_type not in rates:
                continue
            if since is not None and r.created_at >= since:
                continue
            now = float(rates[r.field_type])
            if now >= float(r.baseline_rate) * 0.9:
                out.append({"kind": "retract", "rule_id": r.rule_id,
                            "title": f"効果なし: {r.field_type} のルールを取り消す",
                            "rationale": f"承認時の修正率 {float(r.baseline_rate):.1%} → 今期 {now:.1%}（1割以上の改善なし）。ルール:「{r.text[:60]}」",
                            "evidence": {"rule_id": r.rule_id, "field_type": r.field_type, "baseline_rate": r.baseline_rate, "rate": now}})
        return out


def _confusion_of(d: dict) -> tuple[str, str, str]:
    """提案の根拠から (項目種別, from, to) を取り出す。evidence に無ければ題名／ルール文の「X」「Y」から推定。"""
    import re as _re
    ev = d.get("evidence") or {}
    ft = str(ev.get("field_type") or ev.get("key") or "")
    a, b = str(ev.get("from") or ""), str(ev.get("to") or "")
    if not ft:
        m = _re.search(r"(applicant|deliveries)\.[a-z_]+", (d.get("title") or "") + " " + (d.get("rule_text") or ""))
        ft = m.group(0) if m else ""
    if not (a and b):
        found = _re.findall(r"「(.)」", (d.get("title") or "") + " " + (d.get("rule_text") or ""))
        if len(found) >= 2:
            a, b = found[0], found[1]
    return ft, a, b


# ---------------------------------------------------------------------- ルール版（オフライン）
def _propose_with_rules(a: dict, policy: dict) -> list[dict]:
    out: list[dict] = []
    for c in a.get("confusions_top", []):
        if c["count"] >= 3:
            out.append({"kind": "rule", "scope": "global",
                        "title": f"{c['field_type']}: 「{c['from']}」を「{c['to']}」と読み違えやすい",
                        "rule_text": f"{_ft_label(c['field_type'])}では手書きの「{c['from']}」が「{c['to']}」である場合が多い。形が似ていれば「{c['to']}」の可能性を優先して確認すること。",
                        "rationale": f"直近の修正で {c['from']}→{c['to']} の置換が {c['count']} 件", "evidence": c})
    am = a.get("auto_missed", {})
    if am.get("count", 0) >= 2:
        cur = float(policy.get("audit_sampling_rate", 0.02))
        out.append({"kind": "policy", "policy_key": "audit_sampling_rate", "policy_to": round(min(0.2, cur * 2), 3),
                    "title": "監査サンプリング率を引き上げ", "rationale": f"自動確定の見逃しが監査で {am['count']} 件見つかった（{am.get('field_types')}）。見逃しの計測精度を上げる",
                    "evidence": am})
    for row in a.get("by_field_type", []):
        if row["n"] >= 30 and row["rate"] >= 0.08:
            out.append({"kind": "rule", "scope": "global", "title": f"{row['key']} の修正率が高い（{row['rate']:.0%}）",
                        "rule_text": f"{_ft_label(row['key'])}は誤読が多い。読みにくい場合は推測せず空文字にし、evidence に読めた部分だけを入れること。",
                        "rationale": f"{row['n']} 件中 {row['corrected']} 件が修正された", "evidence": row})
    return out


_ADK_INSTRUCTION = (
    "あなたは手書き注文書の読み取りシステムの改善担当です。与えられた集計（個人情報は含まない）だけを根拠に、"
    "propose ツールで改善提案を最大5件返してください。提案は2種類: "
    "kind='rule'（抽出モデルに渡す日本語の読み取りルール。rule_text は1〜2文で具体的に、scope は 'global' か 'format:<id>'）、"
    "kind='policy'（policy_key と policy_to。許可キー: " + ", ".join(ALLOWED_POLICY_KEYS) + "。範囲外は却下される）。"
    "根拠の無い提案・集計に現れない主張はしないこと。rationale には集計の数字を引用すること。"
    "rule の提案には evidence として {field_type, from, to} を必ず付けること（from→to は confusions_top の混同）。"
    "異体字（髙↔高、﨑↔崎、邊↔辺、齋↔斎 など）の置き換えルールは提案しないこと（システムが根拠と顧客照合で扱う）。"
    "active_rules に同じ項目種別・同じ混同のルールが既にあれば重ねて提案しないこと。"
    "根拠の強さの基準: 読み取りルールは同じ混同（from→to）が3件以上、または項目種別の件数が20件以上で修正率が5%以上のときだけ提案する。"
    "1〜2件の偶発的な誤りからルールを作らないこと。提案が無い場合は空のリストで propose を呼ぶこと。"
)


async def _propose_with_adk(analysis: dict, policy: dict) -> list[dict]:
    import json
    from google.adk.agents import Agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    got: dict[str, list] = {}

    def propose(proposals: list[dict]) -> dict:
        """改善提案を登録する。proposals: [{kind, title, rationale, rule_text?, scope?, policy_key?, policy_to?}]"""
        got["p"] = proposals[:5]
        return {"ok": True, "count": len(got["p"])}

    from app.config import make_adk_model
    agent = Agent(name="ocr_reflection", model=make_adk_model(), instruction=_ADK_INSTRUCTION, tools=[propose])
    runner = InMemoryRunner(agent=agent, app_name="ocr_trust")
    session = await runner.session_service.create_session(app_name="ocr_trust", user_id="reflection")
    cur = {k: _get_path(policy, k) for k in ALLOWED_POLICY_KEYS}
    text = "集計:\n" + json.dumps(analysis, ensure_ascii=False, indent=1) + "\n\n現在のポリシー値:\n" + json.dumps(cur, ensure_ascii=False)
    async for _ in runner.run_async(user_id="reflection", session_id=session.id,
                                    new_message=types.Content(role="user", parts=[types.Part(text=text)])):
        pass
    return got.get("p") or _propose_with_rules(analysis, policy)


# ---------------------------------------------------------------------- 小道具
def _ft_label(ft: str) -> str:
    names = {"zip": "郵便番号", "address": "住所", "name": "氏名", "name_kana": "フリガナ", "phone": "電話番号",
             "organization": "会社名", "product_code": "商品番号", "qty": "数量", "noshi_name": "のし名入れ"}
    who = "ご依頼主" if ft.startswith("applicant") else "お届け先"
    return f"{who}の{names.get(ft.rsplit('.', 1)[-1], ft)}"


def _get_path(d: dict, key: str) -> Any:
    cur: Any = d
    for k in key.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set_path(d: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = d
    for k in parts[:-1]:
        cur = cur.setdefault(k, {})
    cur[parts[-1]] = value
