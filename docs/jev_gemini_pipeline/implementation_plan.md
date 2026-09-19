# Jev × Gemini 二段構え推敲パイプライン 実装計画

System One モデルである **TypeSafe (Jev)** の高速・客観的な診断結果を、System Two である **Google Gemini**（Google Developer Program の月額クレジットを活用）に渡し、記事の弱点をピンポイントで解決する自動リライト・改善パイプラインを構築します。

---

## アーキテクチャと処理フロー

```
[記事テキスト (State)]
        │
        ▼ (Step 1: System One 高速診断 / 約0.6秒)
【TypeSafe Jev】
  ├── 導入の引き (Score)
  ├── 文体トーン (Choice)
  ├── 難易度 (Score)
  ├── 具体例・学びの有無 (Noul)
  └── 誤解・炎上リスク (Noul)
        │
        ▼ (Step 2: 課題・改善ポイントの自動抽出)
【コード側の判定ロジック】
  ・引きが弱い（スコア < 2.0）
  ・具体例が不足（Noul < 0.5）
  ・誤解リスクが高い（Noul > 0.6）等
        │
        ▼ (Step 3: System Two 推論 & リライト生成)
【Google Gemini 2.5 Flash / Pro】
  Jevの指摘事項をプロンプトとして受け取り、
  弱点を解消した「Before / After 改善リライト案」を生成
        │
        ▼
【Web UI に美しく表示】
  ・元の文章 vs 改善リライト案
  ・Gemini による改善ポイント解説
```

---

## 提案する変更・作成内容

### 1. 依存ライブラリの追加
- Google 公式 SDK `google-genai` をインストール
- システム環境変数 `GEMINI_API_KEY`（既存の設定）を自動利用

### 2. バックエンド拡張 (`backend/app.py`)
- `google-genai` クライアントの初期化
- 新規エンドポイント `POST /api/improve_with_gemini` の追加
  - Jev の評価結果から弱点・改善項目をコードで自動判定
  - Gemini 2.5 Flash に「客観的診断結果に基づいたピンポイント改善」を指示
  - 改善リライト案と解説テキストを JSON で返却

### 3. フロントエンド拡張 (`frontend/`)
- 結果表示エリアに「✨ Gemini で弱点を改善・リライト」アクションボタンを追加
- Gemini のリライト結果を表示する「改善サマリー＆リライト案カード」コンポーネントを追加
  - Jev が指摘した課題バッジ
  - 改善後のリライト文章（ワンクリックコピー機能付き）
  - Gemini による推敲アドバイス・解説

---

## 検証手順
1. `google-genai` のインストールと API 接続確認
2. バックエンド API `/api/improve_with_gemini` の単体疎通テスト
3. Web UI 上でブログ記事を評価後、Gemini でリライト生成を実行し、表示と動作を確認
