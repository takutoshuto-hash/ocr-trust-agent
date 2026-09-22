"""修正予測ルーター（第1段の機械学習）。

人の確定履歴（TrainingRecord）から「この項目は修正されるか」を学習し、
予測確率が target_error_rate 未満なら自動確定の候補にする。
モデルは小さい勾配ブースティング（sklearn HistGradientBoosting）。学習データが少ないうちは None を返し、
台帳の判定だけで動く。閾値は「目標誤り率」から検証データ上で逆算する。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import joblib
import numpy as np

from app.schemas import TrainingRecord
from .features import FEATURE_NAMES, to_vector


class CorrectionRouter:
    def __init__(self, model_dir: Path, min_samples: int = 200, target_error_rate: float = 0.005):
        self.model_dir = Path(model_dir)
        self.min_samples = min_samples
        self.target_error_rate = target_error_rate
        self.model = None
        self.threshold: Optional[float] = None
        self.trained_on: int = 0
        self._load()

    # ---- 永続化 ----
    @property
    def path(self) -> Path:
        return self.model_dir / "router.joblib"

    def _load(self) -> None:
        if self.path.exists():
            d = joblib.load(self.path)
            self.model, self.threshold, self.trained_on = d["model"], d["threshold"], d["n"]

    def _save(self) -> None:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": self.model, "threshold": self.threshold, "n": self.trained_on, "features": FEATURE_NAMES}, self.path)

    # ---- 学習 ----
    def train(self, records: list[TrainingRecord]) -> dict:
        """戻り値: 学習サマリ（監査ログ用）。サンプル不足なら {'trained': False}。"""
        records = [r for r in records if r.verified]   # 人が実際に目視したラベルだけ（自己強化の防止）
        if len(records) < self.min_samples:
            return {"trained": False, "n": len(records), "min_samples": self.min_samples}
        X = np.array([to_vector(r.features) for r in records], dtype=float)
        y = np.array([1 if r.corrected else 0 for r in records], dtype=int)
        if y.sum() == 0 or y.sum() == len(y):
            return {"trained": False, "n": len(records), "reason": "ラベルが片側のみ"}

        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.model_selection import StratifiedKFold, cross_val_predict

        def make():
            return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, random_state=42)

        # 閾値は out-of-fold 予測（全データ）で決める。25% の検証分割だと少量期に 1 件の誤りで閾値が 0 に振れて不安定なため
        n_splits = 5 if y.sum() >= 5 else 2
        p = cross_val_predict(make(), X, y, cv=StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42), method="predict_proba")[:, 1]
        threshold = self._threshold_for_target(p, y)
        model = make().fit(X, y)

        self.model, self.threshold, self.trained_on = model, threshold, len(records)
        self._save()
        auto_mask = p < threshold
        return {
            "trained": True, "n": len(records), "threshold": round(float(threshold), 4),
            "cv_auto_rate": round(float(auto_mask.mean()), 4),
            "cv_auto_error_rate": round(float(y[auto_mask].mean()) if auto_mask.any() else 0.0, 4),
        }

    def _threshold_for_target(self, p: np.ndarray, y: np.ndarray) -> float:
        """out-of-fold 予測上で、自動確定集合の誤り率の上側信頼限界（Wilson 95%）が target 以下になる最大の閾値。

        単純な標本誤り率だと n が小さいうちに楽観的になるので、信頼区間の上限で判定して安全側に倒す。
        """
        order = np.argsort(p)
        ps, ys = p[order], y[order]
        cum_err = np.cumsum(ys)
        n = np.arange(1, len(ys) + 1)
        z = 1.96
        phat = cum_err / n
        upper = (phat + z * z / (2 * n) + z * np.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / (1 + z * z / n)
        ok = np.where(upper <= self.target_error_rate)[0]
        if len(ok) == 0:
            return 0.0
        k = ok.max()                       # 条件を満たす最大の集合
        # 閾値はその集合の最大 p と次の p の中点（同点の取りこぼしを避ける）
        nxt = ps[k + 1] if k + 1 < len(ps) else ps[k] + 1e-6
        return float((ps[k] + nxt) / 2)

    # ---- 推論 ----
    def predict(self, features: dict[str, float]) -> Optional[float]:
        if self.model is None:
            return None
        return float(self.model.predict_proba(np.array([to_vector(features)], dtype=float))[0, 1])

    def is_auto(self, p: Optional[float]) -> Optional[bool]:
        if p is None or self.threshold is None:
            return None
        return p < self.threshold
