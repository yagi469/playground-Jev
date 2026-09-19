# [実装計画] 論文本文（HTML）の要所抽出注入 ＆ 数理構造Mermaidダイアグラム自動生成

## 概要
現在 `playground-Jev/daily_paper_blogger.py` では、arXiv の「アブストラクト（英語200〜300語）」のみを Gemini に提供しています。
これを一歩進め、**「arXiv 公式 HTML / ar5iv からの論文本文（Introduction / 主要定理 / Conclusion）の自動抽出・生データ注入」** および **「数理構造・双対性マップの Mermaid ダイアグラム自動生成」** を導入することで、記事の専門的解像度と視覚的説得力を極限まで高めます。

---

## 提案する新機能

### 1. arXiv 本文（HTML / ar5iv）の重要セクション自動抽出
- **取得対象**: `https://arxiv.org/html/{arxiv_id}` （フォールバック: `https://ar5iv.labs.arxiv.org/html/{arxiv_id}`）
- **抽出アルゴリズム (`fetch_arxiv_paper_content`)**:
  - BeautifulSoup4 を使用し、HTML内のセクション構造（`<section class="ltx_section">`）を解析。
  - 以下の3大重要セクションを自動同定・抽出：
    1. **Introduction / Motivation**（研究背景・従来手法の限界・本論文の主目的）
    2. **Main Results / Model / Theorems**（論文の核心となる数学的定義・主定理・数理モデル）
    3. **Conclusion / Discussion / Outlook**（物理的意義・未解決の課題・今後の展望）
  - 各セクションから合計 3,000〜5,000 文字程度（Geminiのコンテキストとして最適なサイズ）を抜粋。
  - ※ HTML が存在しない論文（古い論文等）の場合は、従来通りアブストラクトのみで安全にフォールバック。

### 2. 数理構造・理論マップの Mermaid ダイアグラム自動生成
- `yagibrary`（`src/pages/posts/[slug].astro`）には既にクライアントサイドの Mermaid レンダリング機構が組み込まれています。
- Gemini の執筆プロンプト（初回執筆 ＆ リライト用）に以下の厳密な Mermaid ガイドラインを追加：
  - 論文が扱う理論の対応関係（例: 4d SCFT ➡️ 2d VOA）、S双対性の軌道、真空の分岐図、アノマリーの相殺フローなどを表す Mermaid 図（`graph TD` または `graph LR`）を**本文中に必ず1点以上出力**させる。
  - Mermaid構文エラーを防止するためのエスケープ規則（特殊文字・括弧を含むノード名は `["..."]` でクォートする等）をプロンプトで徹底。

### 3. Jev による推敲評価（Evaluator-Optimizer）の連携
- `verify_post_with_jev` の診断項目に、
  - 「Mermaid ダイアグラムによって理論の構造が視覚的にわかりやすく整理されているか」
  - 「論文本文の具体的な定理や計算機構が反映されているか」
  のチェックを追加。
- 欠落している場合は、Jev の改善指示として Gemini にリライト（Evaluator-Optimizer ループ）を要求。

---

## 変更対象ファイル

### `playground-Jev`
#### [MODIFY] [daily_paper_blogger.py](file:///c:/Users/user/Dev/playground-Jev/daily_paper_blogger.py)
- `fetch_arxiv_paper_content(arxiv_id: str)` 関数の新設（HTML スクレイピング・重要セクション抽出）
- 論文情報辞書（`paper`）に `full_text_excerpt`（本文抜粋）を格納
- `write_blog_post_with_gemini()` および `rewrite_blog_post_with_gemini()` のプロンプト更新（論文本文抜粋の提供 ＆ Mermaid ダイアグラム出力ルール）
- `verify_post_with_jev()` の設問・評価ロジックの強化（Mermaid図・具体定理のチェック）

---

## 検証計画

### 1. 動作確認テスト
- 特定の arXiv 論文（例: `2006.13892` または `2609.20631`）を指定してスクリプトを実行。
- コンソール上で以下を確認：
  1. arXiv HTML から Introduction, 主結果, Conclusion が正常に抽出されること
  2. Gemini が本文抜粋に基づき、より具体的な定理・数式と Mermaid ダイアグラムを生成すること
  3. Jev が Mermaid 図と数理の具体性を評価し、合格すること
  4. 生成された Markdown を Astro でビルドし、Mermaid 図が正常にレンダリングされること
