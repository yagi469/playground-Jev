# 実装計画: Antigravity SDK 移行 (playground-Jev & yagibrary)

## 1. 目的と背景
現在、`playground-Jev` および `yagibrary` の Python スクリプトは `google-genai` を介して Google AI Studio の API キー（`GEMINI_API_KEY`）を直接使用しており、API 側の利用枠や従量課金対象となっています。
本改修では、Antigravity デスクトップ環境の認証（`oauth-personal`）と利用枠（クォータ）を活用できる `google-antigravity` SDK を導入し、ローカル実行時の課金を回避しつつクォータ枠を有効活用できるようにします。

## 2. アーキテクチャと設計方針

### A. 透過的なラッパー設計
既存のジェネレーターやリーダー（`doc_generator.py`, `doc_reader.py` など）は `res = client.models.generate_content(...)` という同期的な呼び出しを行っています。
これを一斉に全面改修すると影響範囲が広いため、[`config.py`](file:///c:/Users/user/Dev/playground-Jev/config.py) に以下の仕組みを構築します：

1. **`generate_text(prompt, system_instruction=None, model=None)` ヘルパー**:
   - `google.antigravity` がインポート可能、かつ環境が整っている場合は **Antigravity SDK (`Agent`)** を優先。
   - `asyncio.run` または実行中のイベントループに対応した同期実行ヘルパーを介して文字列を取得。
   - 万が一のエラー時や Antigravity 未ログイン時は、自動的に従来の `genai.Client()` にフォールバック。

2. **モデル互換性**:
   - Antigravity 側は `Gemini 3.8 Flash` 等のクォータをそのまま活用。

### B. yagibrary 側の設計
- [`yagibrary/scripts/ai_article_editor.py`](file:///c:/Users/user/Dev/yagibrary/scripts/ai_article_editor.py) において、ローカル実行時（`google-antigravity` が利用可能な環境）は Antigravity Agent を優先して呼び出し、GitHub Actions 等の CI 環境では既存の `GEMINI_API_KEY` による `genai.Client` を利用するハイブリッド構成とします。

## 3. 実装手順
1. **仮想環境パッケージ導入**: `playground-Jev/.venv` に `google-antigravity` をインストール。
2. **`playground-Jev/config.py` 改修**:
   - `antigravity` クライアント初期化ロジックの追加。
   - `generate_text_sync` / `generate_text_async` 関数の追加。
3. **テスト検証**:
   - `playground-Jev` ルートで検証用スクリプトを実行し、正常に Antigravity のクォータ枠で回答が得られることを確認。
4. **`yagibrary/scripts/ai_article_editor.py` 改修**:
   - `call_gemini_to_edit_article` をハイブリッド化。
5. **完了確認 (`walkthrough.md`)**:
   - 各ファイルの変更点と動作確認手順を整理。
