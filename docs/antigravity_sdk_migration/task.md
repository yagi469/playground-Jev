# タスクリスト: Antigravity SDK 移行 (playground-Jev & yagibrary)

## 概要
Google AI Studio の API キー従量課金ではなく、Antigravity の標準クォータ（ローカル認証）を消費して動作するよう、`playground-Jev` および `yagibrary` の Python スクリプト群を `google-antigravity` SDK に移行・統合する。

## タスク一覧

- [x] **1. 環境準備**
  - [x] `playground-Jev` の仮想環境 (`.venv`) に `google-antigravity` をインストール
  - [x] 仮想環境内での SDK 動作確認テスト

- [x] **2. playground-Jev の基盤実装 (`config.py`)**
  - [x] `google.antigravity.Agent` の遅延初期化およびセッション管理の実装
  - [x] 同期・非同期両対応の共通テキスト生成ヘルパー (`generate_with_antigravity`) の実装
  - [x] 既存コードとの互換性・フォールバック機構の確保（SDK利用可能時はAntigravity、不可時は従来のGenAI API）

- [x] **3. playground-Jev の動作検証**
  - [x] サンプルプロンプトによる生成検証テスト
  - [x] 各ジェネレーター（`generators/doc_generator.py` 等）の接続確認

- [x] **4. yagibrary の移行 (`scripts/ai_article_editor.py`)**
  - [x] `call_gemini_to_edit_article` を Antigravity SDK 優先の設計に改修
  - [x] ローカル環境での動作テスト

- [x] **5. ドキュメント作成と検証まとめ**
  - [x] 修正内容の確認ドキュメント (`walkthrough.md`) の作成
