# 提出までの手順書（9/26 時点）

締切 2026-10-15 23:59。審査は main ブランチ。デプロイは 12/1 まで維持。
毎回の作業の型: **相談 → 決定を `HANDOVER.md` に記録 → 実装 → テスト → push**。Mac 側の手順はコマンドをそのまま貼る。

判断の軸（審査基準に対応）
1. 人の確認がどれだけ減ったか、そのとき自動確定の間違いがどれだけか（有効性）
2. エージェントが自分で判断し、自分で戻すこと（自律性）
3. 3 ファイルとキー 1 行で他業種に移ること、費用（品質・拡張性）

## 第 1 段階: 記録一致を確定根拠にする（9/27〜9/29）

| # | やること | 誰が | 完了の印 |
|---|---|---|---|
| 1-1 | 常連のキーを様式ファイルに宣言（`record_key`。fax_v1 は `applicant.phone`）。コードの電話番号決め打ちを置き換える | Claude | `tests/test_history.py` にキー差し替えのテスト |
| 1-2 | 検証に全部合格し、読んだ値がその送り主または同じ顧客の確定値と完全一致した項目は、判定・台帳を待たず自動確定 | Claude | 2 周評価で常連の要確認が下がる（見込み 26.2% → 約 17%） |
| 1-3 | 確認画面の言葉: 「この依頼主は 4 回目で、記録と一致しています」。「マスタ」は使わない | Claude | `tests/test_review_wording.py` |
| 1-4 | Mac で全テスト → 本番 Gemini で 2 周評価をやり直す（修正後のコードの数字にする） | 本人 | `eval/out/handwriting_eval_r2.csv` を push |
| 1-5 | 数字を HANDOVER・`docs/SUBMISSION.md`・`docs/ARTICLE.md` に差し替え | Claude | 3 文書の数字が一致 |

Mac の手順（1-4）
```bash
cd ~/ocr-trust-agent && source .venv/bin/activate && git pull
python -m pytest -q
export GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_GENAI_USE_GCLOUD_TOKEN=1 GOOGLE_CLOUD_PROJECT=ocr-trust-agent GOOGLE_CLOUD_LOCATION=global
python eval/run_eval.py --dir data/measurement/handwriting --rounds 2 --set min_samples_for_sender=3 --report eval/out/handwriting_eval_r2.csv
git add eval/out/handwriting_eval_r2.csv && git commit -m "Real handwriting evaluation after record-match rule" && git push
```

## 第 2 段階: 費用と 2 回目の読み取りの省略（9/30〜10/2、時間があれば）

| # | やること | 誰が | 完了の印 |
|---|---|---|---|
| 2-1 | 1 回目の読み取りを先に記録と突き合わせ、一致しなかった欄だけ 2 回目を読む | Claude | 呼び出し回数が監査ログに残る |
| 2-2 | 1 枚あたりのトークン・費用を実測（初見の紙と常連の紙で比較） | 本人（Mac で実行）+ Claude（集計） | 記事「費用」の節が埋まる |
| 2-3 | 費用の節を記事・提出文に書く（本人の方針: 最終段階でまとめて） | Claude | ― |

見送る判断: 10/1 の時点で第 3 段階に入れていなければ、2-1 は設計として書くだけにして数字は出さない。

## 第 3 段階: 画面と文書の仕上げ（10/1〜10/4）

| # | やること | 誰が |
|---|---|---|
| 3-1 | 画面の見た目（配色・動線）。言葉は済み。確認画面・朝のブリーフィング・ダッシュボードの 3 画面 | Claude、本人が確認 |
| 3-2 | 現場の言葉 300 字（1 日の枚数・繁忙期・担当人数・事故の経験）を本人が書く → 提出文の冒頭に入れる | 本人 |
| 3-3 | 英語のプロジェクト名を決める（例: OCR Trust Agent のまま） | 本人 |
| 3-4 | アーキテクチャ図の数字を差し替え、PNG を再生成（`docs/render.html` をブラウザで開いて保存） | Claude が SVG、本人が PNG |
| 3-5 | 提出文（`docs/SUBMISSION.md`）と記事（`docs/ARTICLE.md`）の推敲。他業種の表（紙・キー・減る項目）を入れる | Claude、本人が読む |
| 3-6 | オフィスアワー用の質問一覧を作る（記事の草稿、動画の筋、審査で見られる点） | Claude |

## 第 4 段階: オフィスアワーと動画（10/5〜10/9）

| # | やること | 誰が |
|---|---|---|
| 4-1 | 10/5（月）18:00〜19:30 オフィスアワー。草稿・動画の筋・質問一覧を持ち込む | 本人 |
| 4-2 | 講評を HANDOVER に記録し、直す所を決める | 本人 → Claude |
| 4-3 | 3 分動画の撮影。台本は `docs/DEMO.md`。山場は `python scripts/demo_story.py run --auto`（事故 → コツの提案 → 効果測定で取り消し → 復旧）。同じ結末が毎回出る | 本人 |
| 4-4 | 動画の構成: 課題（52 秒/件）30 秒 → 仕組み 40 秒 → 山場 70 秒 → 実物の数字と限界 30 秒 → 他業種 10 秒 | ― |
| 4-5 | YouTube に限定公開でアップし、URL を控える | 本人 |

## 第 5 段階: 提出（10/10〜10/13。期限の 2 日前に出す）

| # | やること | 誰が |
|---|---|---|
| 5-1 | 作業ブランチを main に取り込む（`git checkout main && git merge claude/eager-johnson-emowfr && git push`） | 本人 |
| 5-2 | `MIN_INSTANCES=1 bash scripts/deploy.sh` でデプロイ。URL が開くことを確認 | 本人 |
| 5-3 | `/review` に確認待ちを数枚残す（審査員が確認画面を触れるように） | 本人 |
| 5-4 | Zenn ダッシュボード: 概要（277 字の下書きあり）、プロジェクト紹介（`docs/SUBMISSION.md`）、アーキテクチャ図、動画 URL、GitHub 連携、デプロイ URL、動作確認の方法、Basic 認証の ID とパスワード（`gcloud secrets versions access latest --secret=REVIEW_PASSWORD`） | 本人 |
| 5-5 | 提出前チェック（LICENSE、期間中の実装、勤め先のコード・データなし、main が最新） | Claude（`gc-hackathon-vol5` スキルの一覧で確認） |
| 5-6 | 提出。以後 12/1 まで main とデプロイを触らない。続きは別ブランチ | 本人 |

## 第 6 段階: 提出後（10/16〜12/1）

- デプロイを維持（Cloud Run の最小インスタンス 1 のまま。費用は月数千円）
- Zenn 記事の公開（任意。提出文より詳しく、限界と費用を含める）
- 1 次・2 次審査の結果連絡を待つ（10 月中旬〜下旬）。最終ピッチに進んだら台本を短く作り直す

## やらないこと（決定済み）

- Jev は不採用。姓辞書は提出後。全国郵便番号一覧は提出後（限界として書く）
- v2 の 14 日の数字は再実行しない。「その後 3 つの穴を直した」と添える
- 費用の議論は最終段階（第 2 段階の 2-3）まで持ち越す
- 勤め先のコードとデータは使わない。データはすべて架空
