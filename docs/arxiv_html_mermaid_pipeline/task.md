# タスクリスト: arXiv本文要所抽出 ＆ Mermaidダイアグラム自動生成パイプライン

- [x] **arXiv 本文自動抽出機能の実装**
  - [x] `BeautifulSoup` による HTML パーサー導入
  - [x] `https://arxiv.org/html/{id}` および `ar5iv` からの本文取得関数 `fetch_arxiv_paper_content` 実装
  - [x] Introduction, Main Results/Theorems, Conclusion の重要セクション抽出とプロンプト注入
- [x] **Mermaid ダイアグラムの自動描画 ＆ Jev 診断機能の実装**
  - [x] Gemini 執筆プロンプトへの Mermaid ダイアグラム出力ルール（ルール5）追加
  - [x] Jev (System One) による `mermaid_visualization` 設問と足切りロジックの追加
  - [x] リライト時の重点改善指示（`need_mermaid_map`）対応
- [x] **パイプライン全体の統合と実機検証**
  - [x] `run_daily_pipeline` および `run_targeted_pipeline` への組み込み
  - [x] `arXiv:2006.13892` を用いた動作テスト（本文抽出・Mermaid生成・Jev一発合格を確認）
