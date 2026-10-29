# syuusyuu — 毎日の論文ダイジェスト

GitHub Actions で毎朝 7:07 (JST) に alphaXiv の話題論文を取得し、Claude が選んだ論文（既定8本）の**本文**を読み、新聞形式の詳しい日本語解説（A4 で約6〜15枚）を PDF にして `digests/` にコミットします。

## セットアップ
1. リポジトリの Settings → Secrets and variables → Actions に `ANTHROPIC_API_KEY` を登録
2. Actions タブ → "Daily paper digest" → **Run workflow** で手動テスト

## 調整
- 本数: 手動実行時の入力 / env の `N_PAPERS`（既定 8。1本あたり A4 1〜2枚）
- モデル: `CLAUDE_MODEL`（既定 claude-opus-5-5。安くするなら claude-sonnet-5-5）
- 分野（フォールバック時）: `digest.py` の `CATEGORIES`

alphaXiv の取得に失敗した日は arXiv 新着 150 本から Claude が選定します。
