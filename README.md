# OCR Trust Agent — 信頼が育つ手書き注文書エージェント

手書き注文書（FAX・ハガキ）を **抽出 → ジャッジ → 自律レベル判定** で処理し、人は要確認項目だけを直す。
人の修正はそのまま教師データになり、**数をさばくほど要確認が減る**。自律の範囲はポリシーと監査ログで統制する。

第5回 Agentic AI Hackathon with Google Cloud 応募作品（テーマ: 「使える」エージェント／ガバナンスとセキュリティ）。

## 何が新しいか

- **信頼が育つ**: 項目種別 × 送り主 / 様式 / 全体 の3階層トラスト台帳が、承認実績で自律レベル（L0→L1→L2）を昇格し、修正1回で即降格
- **学習**: 「この項目を人が直すか」を予測する分類器を毎晩再学習。目標誤り率（例 0.5%）から自動確定の閾値を逆算
- **新規記入者にも効く**: 履歴ゼロで動く決定的検証（郵便番号↔住所、商品マスタ、電話桁…）と二重読み取りの一致が主エンジン。送り主ごとの few-shot はリピーターへの加点
- **ガバナンス**: `policy.yaml` で「住所は絶対に L2 にしない」「新規送り主は氏名・住所を必ず人が見る」「1日の予算」を宣言。全判断を監査ログに残し、後から再現できる
- **ハルシネーションを止める**: 欄のインク量と値の整合（空欄なのに値が返った＝創作）、根拠文字列との整合、二重読み取りの一致。自己申告の自信度には頼らない
- **機密とコストは運用プロファイルで切替**: `lean`（現場向け: Flash・機密欄マスク・保持期限）／`secure`（高機密向け: VPC 内 / オンプレの Gemma、画像を外に出さない）。抽出器だけが差し替わる

## 構成（Google Cloud）

Cloud Run（FastAPI）／Gemini API（構造化抽出・二重読み取り）／Agent Development Kit（説明エージェント）／Firestore（台帳・教師データ・監査）／Cloud Storage（画像）／Nano Banana（合成帳票の生成・予定）

詳細: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## セットアップ

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -e ".[dev]"
cp .env.example .env                                  # GEMINI_API_KEY を入れると実画像を読む。空ならモック
pytest
```

## 動かす

```bash
# 手書き風フォント（Google Fonts, OFL: Yomogi / Zen Kurenaido / Klee One / Hachi Maru Pop）を取得
bash scripts/fetch_fonts.sh

# 合成帳票（正解付き）を生成。1枚1筆跡、文字ごとに大きさ・傾き・濃さ・位置がゆらぐ FAX 風
python data/synthetic/generate_forms.py --n 200

# 評価: 項目別の正解率・要確認率・自動確定の誤り率
python eval/run_eval.py --limit 100

# 日次シミュレーション: 要確認率が日ごとに下がる曲線を CSV に
python eval/simulate_days.py --days 14 --per-day 300

# API + 確認画面
uvicorn app.main:app --reload    # http://127.0.0.1:8000/review
```

## デプロイ

```bash
gcloud secrets create GEMINI_API_KEY   --data-file=- <<< "$GEMINI_API_KEY"
gcloud secrets create REVIEW_USER      --data-file=- <<< "judge"
gcloud secrets create REVIEW_PASSWORD  --data-file=- <<< "$(python -c 'import secrets;print(secrets.token_urlsafe(18))')"
gcloud firestore fields ttls update expires_at --collection-group=ocr_trust_forms  --enable-ttl
gcloud firestore fields ttls update expires_at --collection-group=ocr_trust_images --enable-ttl
STORE_BACKEND=firestore bash scripts/deploy.sh
```

デプロイ先は Basic 認証（`/health` のみ公開）。審査用の資格情報は提出時に添付する。

## ディレクトリ

```
app/
  extract/    Gemini 抽出器 / モック抽出器
  judge/      決定的検証ツール・ジャッジ・ADK 説明エージェント
  trust/      トラスト台帳・policy.yaml
  learn/      特徴量・修正予測ルーター
  store/      Memory / Firestore
  pipeline.py 受付→判定→確定→学習
  main.py     FastAPI（/forms, /review, /metrics, /audit, /ledger）
data/master/  商品マスタ・郵便番号（サンプル）
data/synthetic/ 合成帳票ジェネレータ
eval/         評価・日次シミュレーション
```

## 個人情報の扱い

実帳票は使わない。デモ・評価はすべて合成データ。実運用時は「入力を学習に使わない」有料枠、保存期間、アクセスログを前提にする。
