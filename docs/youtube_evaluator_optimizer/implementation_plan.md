# YouTube記事化 Evaluator-Optimizer（自律推敲ループ）実装計画

YouTube動画から NotebookLM 経由で生成されたブログ記事に対して、TypeSafe Jev による品質採点と Gemini によるフィードバック駆動型リライトループ（Evaluator-Optimizer）を導入し、客観的サマリーにとどまらない高品質な解説記事（合格水準: 9.2〜10点以上）へ自動改善する仕組みを構築します。

## ユーザー確認事項
- リライトエンジンとして、既存パイプラインと同様に Google Gemini（`gemini-2.5-flash` / `gemini-1.5-pro` 等）を使用します（`.env.local` の `GEMINI_API_KEY` を利用）。
- デフォルトで最大2回のリビジョンループ（Round 1: NotebookLM初稿 ➔ Round 2〜3: Gemini推敲）を行い、合格スコアに達した時点で完了します。

## 変更内容

### `playground-Jev`

#### [MODIFY] [youtube_to_blog.py](file:///c:/Users/user/Dev/playground-Jev/youtube_to_blog.py)
- **Gemini クライアントの統合**: `google.genai` を用いたリライト機能の実装。
- **Jev 検証モジュールの統合**: `score_post.py` の `verify_post_with_jev` を呼び出し。
- **リライト関数 `rewrite_youtube_blog_post_with_gemini` の追加**:
  - Jev の診断フィードバック（`need_pedagogical_steps`, `need_sharp_opinion`, `need_math_details`, リスク値）をプロンプトに動的注入。
  - 元動画の事実関係（トピックやタイムライン）を保持しつつ、「行間・前提知識の解説」「筆者独自オピニオン」「数理の具体化」を加筆推敲。
- **Evaluator-Optimizer ループ `refine_youtube_blog_post` の追加**:
  - NotebookLM ドラフト取得 ➔ Jev 採点 ➔ 足切り判定 ➔ Gemini リライト ➔ Jev 再採点。
  - 記事末尾に推敲プロセスレポート（自律改善履歴）を付加。
- **CLI オプションの拡充**:
  - `--optimize` / `--no-optimize`: 自動推敲ループの有効/無効切り替え（デフォルト: 有効）。
  - `--max-revisions`: 最大リライト回数（デフォルト: 2）。

## 検証計画

### 自動・CLI検証
1. 先ほどの Strings 2026 記事（初稿 6.33点）をインプットとしてリライトを実行し、Jev スコアが合格基準（9.2以上または10点以上）に向上することを確認。
2. 記事 [2026-09-21-strings2026-shanghai-highlights.md](file:///c:/Users/user/Dev/yagibrary/src/content/posts/2026-09-21-strings2026-shanghai-highlights.md) を推敲済み原稿で上書き更新。
3. `python score_post.py strings2026` で最終スコアを確認。
