# 引き継ぎ書（2026-09-24 時点）

別のパソコン／別のセッションでこのプロジェクトを続けるための文書。読んで把握したら、削除するかローカルに残すかは自由（機密は含めていない）。
この文書の内容は、本人（首藤卓登）と Claude が 9/21〜9/24 に決めたこと・測ったことの要約で、コードから読み取れないものに絞ってある。

## 1. 何のプロジェクトか

- **第5回 Agentic AI Hackathon with Google Cloud（Zenn）** への応募作品。締切 **2026-10-15 23:59**、オフィスアワー 10/5、最終審査 12/1。
- 審査項目: ①課題の新規性と解決策の有効性 ②自律性・エージェントらしさ ③実装品質と拡張性。Cloud Run 等 + Gemini/ADK 等が必須。提出物 = GitHub リポジトリ、デプロイ URL、アーキテクチャ図つき記事、3分動画。
- 目標は **最優秀賞**。「実際に役に立つもの」を作る。
- 作品名: **OCR Trust Agent ― 信頼が育つ手書き注文書エージェント**。手書き FAX 注文書を Gemini で読み、ジャッジが検証し、根拠のある項目だけ自動確定、残りを人が確認、確定が翌日の根拠になって自動確定の範囲が育つ。
- 会社（まるひで）のコードは流用しない。データはすべて合成（ダミー）。実在の顧客情報は入れない。

## 2. いまの状態（数字）

すべて実 Gemini 2.5 Flash（Vertex AI）、正解付き合成手書き、200 枚/日 × 14 日の運用シミュレーション。

| 実行 | 要確認率（14日目） | 自動確定の真の誤り率 | 備考 |
|---|---|---|---|
| 改善前（基準値） | 51.0%（横ばい） | 0.27% | `eval/out/curve_gemini_200x14_vertex.csv` |
| 改善1回目 | 12.6% | 0.25% | 欠陥2つを発見（下記） |
| **改善後 v2（発表用）** | **10.6%**（6〜10日目 8〜9%） | **0.14%** | `curve_gemini_200x14_v2.csv`、提案 承認47・自動却下30・取り消し34 |
| v2 − 振り返りなし | 8.3%（10日目、実行中） | 0.14% | 振り返りは要確認率にほぼ効かない |
| v2 − 2回目を全面読みに戻す | 24.5%（9日目で停止） | 0.23% | 独立二重読みが主役 |

- 50% の床の正体: 要確認の 97% が「検証合格・二重一致・正解」。氏名・フリガナ・住所・電話の誤り 2〜4% を正解と分ける根拠が無かった（同じモデル×同じ画像の二重読みは誤りが揃う）。
- 破った手: ①2回目を欄切り出し画像で読む（一致したのに誤り 2.09%→0.53%）②相互検証（市外局番↔都道府県、姓↔読み）③送り主履歴・電話番号キーの顧客照合 ④振り返り＋模擬承認者。
- 改善1回目で見つけた欠陥: 修復の再読み取りが2回目と同じ入力で相関 → 「1回目と一致したときだけ採用」に修正／振り返りが矛盾する異体字ルールを積み上げた → 異体字ルール禁止・矛盾/重複の自動却下・項目種別あたり2件・効果を測って取り消し提案（kind=retract）。
- 残る要確認の 8 割は **初見のお届け先氏名**（誤り 3.65%、照合する根拠が無い）。「初見の宛名は人が見る」を限界として正直に書く。
- 2回目の読み取りモデル比較（60枚）: Flash切り出し 0.53% が最良。Lite は相関が切れず、Pro は費用4倍で劣る。思考なし（thinking_budget=0）は切り出し読みの独立性を悪化させるので読み取りの思考は既定のまま。
- 現場の実測: 現行 15分30秒/10枚18件 ≈ 52秒/件、本作初期 39.6秒/件、育った後 7.2秒/枚（台帳はシミュレーション由来と明記）。
- 費用: 14日1本 ≈ 1万円（1枚 ≈ 3円。思考トークンと ADK 行動計画が主因）。クレジット 95,626 円のうち 44,508 円使用（9/24 朝）。**費用の内訳は本人指示で「最終段階」に回す**。トークン実測は `app/extract/usage.py` で自動記録済み。

## 3. 本人の判断・好み（守ること）

- 「振り返り込みの改善後14日で要確認率が下がらなければ発表を見直す」→ **クリア済み**。発表に進む。
- **画面の言葉は現場の事務担当者向け**。専門用語・項目コード禁止（`app/labels.py` を通す）。見た目のデザインは最終段階（10月上旬）にまとめて。確認画面の「要確認の理由」文はまだ専門用語あり。
- 変体字（髙→高）は深掘りして発表しない（対応は入れてある: 根拠からの復元＋顧客照合からの字体復元）。
- 電話番号キーの顧客照合・商品マスタ照合は採用。他業種向けの新規開発はしない。「業種依存は3ファイル（様式JSON・マスタCSV・検証の組み合わせ）」として記事で示す。
- 費用の話は最終段階でまとめて。
- 本人が使う Chrome は用途別（転職・Zenn・GitHub・GCP は Takuto アカウント takutoshuto@gmail.com）。
- 応答は日本語。決めごとは「まず話し合って決めてから記録」。

## 4. 環境

- 本番: Cloud Run `ocr-trust-agent`（asia-northeast1）。URL https://ocr-trust-agent-826941973656.asia-northeast1.run.app 。確認画面 `/review`、ブリーフィング `/briefing`、ダッシュボード `/dashboard`。Basic 認証（ユーザー `judge`、パスワードは Secret Manager `REVIEW_PASSWORD`）。
- GCP プロジェクト `ocr-trust-agent`、gcloud アカウント takutoshuto@gmail.com、Firestore native（asia-northeast1、TTL）、Secret Manager（GEMINI_API_KEY / REVIEW_USER / REVIEW_PASSWORD）、Cloud Scheduler（02:00 再学習、02:30 振り返り）、Vertex AI（location global）。
- 本番の台帳・ルーターは **シミュレーション由来**（v2 の `router.joblib` を同梱）。発表で明記。
- ローカル実行の環境変数: `GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_GENAI_USE_GCLOUD_TOKEN=1 GOOGLE_CLOUD_PROJECT=ocr-trust-agent GOOGLE_CLOUD_LOCATION=global`（gcloud のトークンを5分ごとに自動更新）。ADK は `make_adk_model()` で同じクライアントを使う（ADC 不要）。
- セットアップ手順は README「別のパソコン（Mac / Linux）で続ける」。
- 知識ベース（本人の第二の脳）は Windows 機の外付け E:\claude（`career/02_作業中/log.md`、`hackathon-gcp-vol5/00_concept.md`・`01_measurement.md`・`02_article_outline.md`・`03_video_script.md`）。Mac から読めない場合、この文書と `docs/` が代わり。

## 5. 主要な実行コマンド

```bash
# 14日シミュレーション（実 Gemini、振り返り込み。約10時間・約1万円）
python eval/simulate_days.py --extractor gemini --days 14 --per-day 200 --senders 120 --seed 21 --workers 6 --reflect \
  --out eval/out/curve_X.csv --field-log eval/out/fieldlog_X.csv --export-state eval/out/state_X
python eval/compare_runs.py                 # 実行同士の比較表（何が効いたか）
python eval/analyze_state.py --state eval/out/state_gemini14d_v2 --field-log eval/out/fieldlog_gemini_200x14_v2.csv
python eval/double_read_independence.py --n 60 --variants flash:page,flash:zones   # 二重読みの独立性
python eval/run_eval.py --dir data/measurement/handwriting/scans                   # 実手書きの評価（本人がスキャンを置いたら）
bash scripts/deploy.sh                      # Cloud Run へ
```

## 6. 残りの予定（9/24 時点）

| 日程 | やること |
|---|---|
| 9/24 | 切り分けA（振り返りなし）完走 → `compare_runs.py` で最終表、`docs/architecture.svg` の数字を差し替え → PNG 再生成（`docs/render.html` をローカルで開き canvas → PNG） |
| 9/24〜26 | 本人の実手書き 20〜30 枚（`data/measurement/handwriting/` に印刷用 PDF・一覧・手順あり）を評価。ダッシュボード最終化 |
| 9/27〜10/1 | 画面の仕上げ（現場向けの言葉・配色・動線）、Zenn 記事本文（骨子は E: の `02_article_outline.md`。要点はこの文書の 2 節） |
| 10/2〜10/5 | 3分動画（山場 = 事故注入と復旧、エージェントが自分の提案を取り消す場面）、10/5 オフィスアワー |
| 10/6〜12 | 講評反映・予備 |
| 10/13 | 提出（期限の2日前）。デモ前に Cloud Run の min-instances 1 |

本人にお願い中: 実手書き 20〜30 枚、現場の言葉 300 字（1日の枚数・繁忙期・担当人数・事故の経験）、動画で見せる事故の選択、ライセンス（MIT 想定）とプロジェクト名の英語表記、Zenn の提出フォーム／オフィスアワーの手続き確認。

## 7. 記事・動画の要点（決定済みの筋）

- 冒頭は現場の実測（52秒/件 → 7秒/枚）と「要確認率 50% で止まった理由」→ 独立二重読み・相互検証・履歴照合・振り返りで 10.6%。
- 山場: 改善1回目で振り返りが矛盾ルールを積んで氏名の誤りが下がらなかった → 効果測定と取り消しを入れた v2 で誤り率半減。「エージェントが自分の提案を取り消す」。
- 正直に書く: 合成データ、本人1名計測、振り返りは要確認率には効かず価値は自己修正と統治、初見の宛名は人が見る。
- 拡張性: 業種依存は3ファイルだけ。他業種の対応表（介護・保険・物流・建設・自治体）。
- セキュリティ: 照合はサーバー内、few-shot は Vertex（学習不使用・リージョン固定）、secure プロファイル（VPC 内 Gemma）、顧客テーブル分離・保持期間・削除導線は設計として提示。
