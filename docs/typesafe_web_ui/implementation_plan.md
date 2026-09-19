# TypeSafe (Jev) Web UI プレイグラウンド 実装計画

テキストを入力して「分析」ボタンを押すと、TypeSafe (Jev) の **Choice / Score / Noul** の3つのプリミティブを同時に並列評価し、リッチなアニメーションとビジュアルで確率分布や確信度をリアルタイム可視化できる Web UI を作成します。

---

## 構成とデザイン

### 1. アーキテクチャ
- **バックエンド**: `FastAPI` + `uvicorn` (Python)
  - 既存の `.env.local` と `typesafe-sdk` を利用
  - `/api/evaluate` エンドポイントでテキストと質問定義を受け取り、Jev に問い合わせて結果を JSON 返却
- **フロントエンド**: HTML5 + Vanilla CSS + JavaScript (Modern Glassmorphism / Dark Mode)
  - 外部フレームワークに依存せず、軽量かつ最上級のルック＆フィールを実現
  - Google Fonts (`Inter` + `JetBrains Mono`)
  - スムーズなマイクロアニメーションとグラデーション、メーター表示

### 2. 主要機能
1. **テキスト入力（State）**:
   - 自由入力テキストエリア
   - ワンクリックで試せるプリセット（例:「顧客クレーム」「新機能の提案」「セキュリティ懸念の報告」など）
2. **評価質問（Questions）のプリセット切り替え**:
   - デフォルト: 「カスタマーサポート分析」（担当部署 Choice / 怒り度 Score / 至急対応要否 Noul）
   - 「コンテンツモデレーション / ガードレール」や「コード・設計レビュー」などの切り替え
3. **リッチな結果可視化**:
   - **Choice**: 選定された選択肢、各選択肢の確率バー（%）、Confidence（確信度）バッジ
   - **Score**: 0〜N段階のスコアゲージ、各レベルの確率分布
   - **Noul**: Yes/No 判定の確率パーセントバー
   - **メタデータ**: 入出力トークン数、レスポンス所要時間 (ms)

---

## 変更・作成予定のファイル

- [NEW] `backend/app.py`: FastAPI による API サーバー
- [NEW] `frontend/index.html`: Web UI の HTML
- [NEW] `frontend/style.css`: ガラスモフィズム・ダークモードのモダン CSS
- [NEW] `frontend/app.js`: API リクエストと結果レンダリングロジック
- [NEW] `run_server.py`: バックエンド起動用スクリプト
- [NEW] `docs/typesafe_web_ui/task.md` / `implementation_plan.md`: ドキュメント保存

---

## 検証手順
1. `fastapi`, `uvicorn` をインストール
2. サーバーを起動し、ブラウザで Web UI にアクセス
3. テキストの入力と Jev によるリアルタイム評価の動作確認
