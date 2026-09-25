# チェックの実行手順

このファイルは、参加者のリポジトリを検査するときのagent向け手順です。検査結果は「満たしている項目 / 不足している項目 / 判定できなかった項目」に分けて報告してください。

## レギュレーション適合チェック

開発中のプロジェクト（カレントリポジトリ）が [regulations.md](regulations.md) の必須条件を満たすかを検査します。

### 1. Google Cloud 実行プロダクトの利用確認

以下の痕跡を検索して、実行プロダクト（App Engine / Compute Engine / GKE / Cloud Run / Cloud Run functions / Cloud TPU・GPU）の利用を確認する。

- デプロイ設定: `Dockerfile`, `cloudbuild.yaml`, `app.yaml`, `skaffold.yaml`, Terraform（`google_cloud_run_*`, `google_container_cluster` 等）, `gcloud run deploy` を含むスクリプト・CI設定
- Firebase利用時の判定（[faq.md](faq.md) 参照）: Cloud Functions for Firebase・Firebase App Hosting は条件を**満たす**。Firebase Extension のみでは**満たさない**
- ローカル環境のみでの動作は不可（Google Cloudへのデプロイが必要）。デプロイ先が無い場合は不足として報告する
- コードから判別できない場合は、デプロイ先（ダッシュボードで入力するデプロイURLの実行環境）がどのプロダクトかを参加者に確認する

### 2. Google Cloud AI 技術の利用確認

依存関係とコードから、AI技術（Gemini Enterprise Agent Platform（旧称 Vertex AI） / Gemini API / Gemma / Nano Banana / Agent Development Kit（ADK） / Speech / Vision / Natural Language / Translation）の利用を確認する。

- 依存パッケージ例: `google-cloud-aiplatform`, `google-genai`, `@google-cloud/vertexai`, `@google/genai`, `google-adk` など
- コード内のAPI呼び出し・モデル名（`gemini-` など）・エンドポイント
- 判定の補足（[faq.md](faq.md) 参照）: Gemini APIのAI Studio直接利用・Firebase経由のGemini API利用はいずれも条件を**満たす**

### 3. 報告

- 各必須条件について、確認できた根拠（ファイルパス）とともに充足状況を報告する
- コードからの検出は完全ではないため、検出できなかった場合は「利用していない」と断定せず、利用箇所を参加者に確認する

## 提出前チェックリスト

提出直前の確認として、以下を順に検査・確認します。

1. **GitHubリポジトリが連携されているか**: ダッシュボードのプロジェクト編集ページ（審査向け情報タブ）で、プロジェクトのGitHubリポジトリが連携されているか（最大5つ）を参加者に確認する。リポジトリは公開・非公開を問わない（カレントリポジトリのリモートURLが連携済みリポジトリに含まれているかもあわせて確認するとよい）
2. **レギュレーション適合**: 上記の適合チェックを実行
3. **デプロイURL**: ダッシュボードで入力するデプロイURLを参加者に確認し、可能ならアクセスして応答することを確認する
4. **提出物の要件（規約）**: [submission.md](submission.md) の要件を満たしているか確認する
   - 機能を説明するテキストおよびシステム構成図・デモンストレーションビデオが用意されているか
   - 記載が日本語になっているか
   - OSSを使用している場合、ライセンス情報が提出物に明記されているか（リポジトリのLICENSE・READMEや依存関係の表記を確認）
5. **提出操作**: [submission.md](submission.md) のダッシュボード手順（プロジェクトの提出）を案内し、提出期限（2026年10月15日）を過ぎていないか確認する

チェック完了後、不足項目と対応方法を一覧で報告してください。
