from app.config import settings
from app.schemas import AutonomyLevel
from app.store import MemoryStore
from app.trust import Policy, TrustLedger


def _ledger():
    return TrustLedger(MemoryStore(), Policy.load(settings.policy_path))


def test_new_field_is_L0():
    lvl, _, reasons = _ledger().resolve("deliveries.zip", "S1", "fax_v1")
    assert lvl == AutonomyLevel.L0 and any("実績不足" in r for r in reasons)


def test_promotion_then_demotion():
    L = _ledger()
    for _ in range(50):   # policy: L1 = min_samples 50 / streak 30 / 修正率 ≤ 1%
        L.record("deliveries.zip", "S1", "fax_v1", corrected=False)
    lvl, stat, _ = L.resolve("deliveries.zip", "S1", "fax_v1")
    assert lvl == AutonomyLevel.L1 and stat.streak == 50
    events = L.record("deliveries.zip", "S1", "fax_v1", corrected=True)
    assert events and all(e["to"] == 0 for e in events)
    assert L.resolve("deliveries.zip", "S1", "fax_v1")[0] == AutonomyLevel.L0


def test_new_sender_falls_back_to_global_but_name_needs_review():
    L = _ledger()
    for i in range(60):
        L.record("deliveries.zip", f"S{i}", "fax_v1", corrected=False)
        L.record("deliveries.name", f"S{i}", "fax_v1", corrected=False)
    # 未知の送り主でも zip は format/global 層の実績で L1
    assert L.resolve("deliveries.zip", "NEW", "fax_v1")[0] == AutonomyLevel.L1
    assert L.must_review("deliveries.zip", "NEW") is None
    # name は new_sender_review により必須確認
    assert "新規送り主" in (L.must_review("deliveries.name", "NEW") or "")


def test_judge_fail_records_do_not_count():
    L = _ledger()
    for _ in range(50):
        L.record("deliveries.zip", "S1", "fax_v1", corrected=True, judge_ok=False)
    assert L.stat("deliveries.zip|global").n == 0


def test_never_l2_for_address():
    L = _ledger()
    for _ in range(400):
        L.record("deliveries.address", "S1", "fax_v1", corrected=False)
    assert L.resolve("deliveries.address", "S1", "fax_v1")[0] == AutonomyLevel.L1
