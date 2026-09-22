# 実測用の合成帳票（30枚）

生成: `python data/synthetic/generate_forms.py --n 30 --seed 42 --senders 15 --out data/measurement/out`
一覧: `index.csv`（form_no / ファイル名 / 条件 / お届け先数 / 送り主ID）

| 条件 | 帳票 | 使い方 |
|---|---|---|
| ① 現行（OCR アプリ＋全項目の目視照合） | form_no 1〜10（`out/form_42_00000.png` 〜 `form_42_00009.png`） | 現行の OCR 画面に画像を読ませ、いつもどおり全項目を原本と照合して登録する。開始＝画像を開いた瞬間、終了＝登録を押した瞬間。秒数とお届け先数を `E:\claude\career\02_作業中\hackathon-gcp-vol5\01_measurement_log.csv` に記入 |
| ② 本作（要確認だけ） | form_no 11〜30 | 本番の要確認キュー（https://ocr-trust-agent-826941973656.asia-northeast1.run.app/review ）に投入済み。上から順に「確認する」→ 赤枠の項目だけ原本と照合 → 確定。時間は自動記録される（`submitted.csv` に form_id 対応表） |

注意
- 帳票はすべて合成データ（架空の氏名・住所・電話）。実在の情報は含まない。
- 正解は各 `.json` の `truth` にある。①の登録内容は本番システムに残さないこと（テスト環境か、登録後に削除）。
- 集計は「お届け先1件あたりの秒数」の中央値で比較する。
