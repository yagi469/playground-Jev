#!/usr/bin/env python3
"""
physics_gap_blogger.py
======================
物理学の教科書や論文の「式変形の行間（省略された計算ステップや近似、物理的仮定）」を
Gemini（マルチモーダル）によって大学院・研究レベルの厳密さで補完・導出し、
yagibrary (Astro) 向けの美しいブログ記事として自動執筆・保存するツール。

入力ソース:
  - スクショ画像 (--image <path>) または クリップボードの画像 (--clip)
  - 教科書 / 論文の PDF (--pdf <path>) + ページ指定 (--pages "120-123")
  - 自分の勉強ノート / Markdown (--context <path>)
  - 疑問点 / 対象の式変形 (--question "式(3.14)から(3.15)への導出")
"""

import os
import sys
import io
import re
import argparse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

import yaml
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv(".env.local")
load_dotenv(".env")

try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = None
    ImageGrab = None

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = None
    PdfWriter = None

# yagibrary の posts ディレクトリ（デフォルト保存先）
DEFAULT_YAGIBRARY_POSTS_DIR = os.getenv(
    "YAGIBRARY_POSTS_DIR",
    os.path.normpath(os.path.join(os.path.dirname(__file__), "../yagibrary/src/content/posts"))
)

gemini_client = None


def init_gemini_client():
    """Gemini API クライアントのシングルトン初期化"""
    global gemini_client
    if gemini_client is None:
        try:
            from google import genai
            gemini_client = genai.Client()
        except Exception as e:
            raise RuntimeError(f"Gemini Client 初期化エラー: {e}")
    return gemini_client


# ==============================================================================
# 1. パス解決 & PDF / 画像 / テキスト入力処理
# ==============================================================================
def resolve_file_path(file_path: str) -> str:
    """ローカルファイルパスを柔軟に解決（yagibrary/docs/... や docs/... の指定にも対応）"""
    normalized = file_path.replace("\\", "/").strip()
    clean_rel = re.sub(r"^(?:yagibrary/docs/|yagibrary/|docs/)", "", normalized)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # 1. そのままのパス（絶対パスまたはカレント基準）
        os.path.abspath(normalized),
        os.path.join(script_dir, normalized),
        # 2. yagibrary 基準
        os.path.abspath(os.path.join(script_dir, "../yagibrary", normalized)),
        os.path.abspath(os.path.join(script_dir, "../yagibrary/docs", normalized)),
        os.path.abspath(os.path.join(script_dir, "../yagibrary/docs", clean_rel)),
        os.path.abspath(os.path.join(script_dir, "../yagibrary", clean_rel)),
        # 3. playground-Jev 配下の docs / yagibrary
        os.path.abspath(os.path.join(script_dir, "docs", clean_rel)),
        os.path.abspath(os.path.join(script_dir, "yagibrary/docs", clean_rel)),
    ]

    for c in candidates:
        norm_c = os.path.normpath(c)
        if os.path.exists(norm_c) and os.path.isfile(norm_c):
            return norm_c

    # 4. docs フォルダ配下のサブディレクトリを再帰探索
    target_basename = os.path.basename(normalized)
    search_roots = [
        os.path.abspath(os.path.join(script_dir, "../yagibrary/docs")),
        os.path.abspath(os.path.join(script_dir, "../yagibrary")),
        os.path.join(script_dir, "docs"),
        script_dir,
    ]
    for root_dir in search_roots:
        if os.path.exists(root_dir) and os.path.isdir(root_dir):
            for root, _, files in os.walk(root_dir):
                for f in files:
                    if f.lower() == target_basename.lower():
                        return os.path.normpath(os.path.join(root, f))

    raise FileNotFoundError(f"指定されたファイルが見つかりません: '{file_path}' (探索候補: {candidates})")


def extract_pdf_pages_bytes(pdf_path: str, pages_str: Optional[str] = None) -> Tuple[bytes, str]:
    """PDFから指定ページを抽出してバイナリとラベルを返す"""
    if PdfReader is None or PdfWriter is None:
        raise ImportError("pypdf が必要です。'pip install pypdf' を実行してください。")

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise ValueError(f"PDFファイルにページが存在しません: {pdf_path}")

    writer = PdfWriter()
    selected_indices = set()

    if pages_str and pages_str.strip():
        parts = pages_str.split(",")
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                s_str, e_str = part.split("-", 1)
                start = max(1, int(s_str.strip())) - 1
                end = min(total_pages, int(e_str.strip())) - 1
                for idx in range(start, end + 1):
                    selected_indices.add(idx)
            else:
                idx = int(part) - 1
                if 0 <= idx < total_pages:
                    selected_indices.add(idx)

        sorted_indices = sorted(list(selected_indices))
        if not sorted_indices:
            raise ValueError(f"指定されたページ範囲 '{pages_str}' に有効なページが含まれていません (全 {total_pages} ページ)。")

        for idx in sorted_indices:
            writer.add_page(reader.pages[idx])

        label = f"p.{pages_str} (計 {len(sorted_indices)} ページ / 全 {total_pages} ページ)"
    else:
        # デフォルトは先頭または全体（上限10ページ）
        max_default = 10
        count = min(total_pages, max_default)
        for idx in range(count):
            writer.add_page(reader.pages[idx])
        label = f"1-{count} ページ (全 {total_pages} ページ)"

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), label


def get_clipboard_image_bytes() -> Optional[bytes]:
    """クリップボードから画像を取得して PNG バイト列として返す"""
    if ImageGrab is None:
        raise ImportError("Pillow (PIL) が必要です。'pip install Pillow' を実行してください。")

    img = ImageGrab.grabclipboard()
    if isinstance(img, Image.Image):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    return None


# ==============================================================================
# 2. 過去記事インデックス & 関連リンク機能
# ==============================================================================
def load_existing_posts_index(posts_dir: str) -> List[Dict[str, Any]]:
    """既存のブログ記事インデックスを構築"""
    index = []
    if not os.path.exists(posts_dir):
        return index

    for fname in os.listdir(posts_dir):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]
        filepath = os.path.join(posts_dir, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
            if m:
                meta = yaml.safe_load(m.group(1)) or {}
                title = meta.get("title")
                summary = meta.get("summary", "")
                tags = meta.get("tags", [])
                if title:
                    index.append({
                        "slug": slug,
                        "title": title,
                        "summary": summary,
                        "tags": tags if isinstance(tags, list) else [],
                        "url": f"/posts/{slug}",
                    })
        except Exception:
            continue
    return index


def find_relevant_past_posts(
    current_text: str,
    posts_index: List[Dict[str, Any]],
    max_matches: int = 3,
) -> List[Dict[str, Any]]:
    """物理関連の過去記事から最も関連度の高い記事を抽出"""
    if not posts_index:
        return []

    physics_keywords = {
        "量子力学", "場の量子論", "素粒子論", "超弦理論", "数理物理", "AdS/CFT",
        "超対称", "対称性", "ゲージ理論", "統計力学", "解析力学", "相対論"
    }

    scored = []
    for post in posts_index:
        title = post.get("title", "")
        summary = post.get("summary", "")
        tags = [str(t).lower() for t in post.get("tags", [])]

        score = 0
        for kw in physics_keywords:
            if kw.lower() in title.lower() or kw.lower() in summary.lower() or any(kw.lower() in t for t in tags):
                score += 5
            if kw.lower() in current_text.lower():
                score += 2

        if score > 0:
            scored.append((score, post))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p[1] for p in scored[:max_matches]]


# ==============================================================================
# 3. Gemini による物理行間埋め記事の執筆
# ==============================================================================
def generate_physics_gap_post(
    contents: List[Any],
    question_hint: str = "",
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    preferred_title: Optional[str] = None,
    model_name: str = "gemini-3.8-flash",
) -> str:
    """Gemini を用いて大学院レベルの厳密な物理行間埋めブログ記事を生成"""
    client = init_gemini_client()

    related_context = ""
    if relevant_posts:
        lines = [f"- [{rp['title']}]({rp['url']}) ({rp.get('summary', '')[:80]}...)" for rp in relevant_posts]
        related_context = (
            "\n【当ブログの関連記事（自然に文脈をつなげてください）】\n"
            + "\n".join(lines) + "\n"
        )

    title_instruction = (
        f'title: "{preferred_title}"'
        if preferred_title
        else 'title: "「○○の導出：教科書が省略した式(X)から(Y)への行間を埋める」のような、物理的本質と知的好奇心を突いた日本語タイトル"'
    )

    system_prompt = f"""あなたは場の量子論、数理物理学、超弦理論、理論物理学全般の最前線を探究する一流の理論物理学者兼サイエンスブロガーです。
あなたの読者は物理学の修士課程修了レベル以上の知識を持つ者（または意欲的な研究者・院生）です。

【最重要指針：物理・数学のギャップを徹底的に埋め、難解な解説を解きほぐす】
教科書や論文では、数式の飛躍（「式(A)より直ちに式(B)を得る」）だけでなく、**著者の文章・解説が極めて抽象的でわかりづらい**ことが多々あります。
以下の2つの側面から、徹底的にかみ砕いて解き明かしてください：

1. **数式変形の行間埋め（数理的厳密性）**:
   - ゲージ固定、基底の選択、自然単位系の規約
   - 表面項・境界項をどのような境界条件でゼロとして落としたか
   - どの微小パラメータに関する何次の近似か（テイラー展開、摂動展開、鞍点近似、双極子近似など）
   - 演算子の交換関係 [A, B]、テンソル縮約公式、ディラック行列のトレース公式、積分経路・留数計算などの非自明なステップを省略なしで KaTeX 展開
   - ※「両辺を移項して2で割ると…」のような中学生レベルの四則演算の説明は省き、物理的・数学的な核心の跳躍に集中すること。

2. **難解な概念・定性解説のかみ砕き（物理的直観と言語化）**:
   - 著者が抽象的な専門用語でサラッと述べている定性的な主張について、「要するに物理的に何が起きているのか？」を直観的・幾何学的な描像で解きほぐす。
   - なぜ著者はそのような物理的設定やアナロジーを引いているのか、その背後にある本質的な動機（対称性の要請、因果律、ユニタリティ、自由度の数え上げ等）を明快に言語化する。
   - 読者が「なるほど、著者が言いたかったのはこういう描像だったのか！」と腑に落ちる解説を提供する。

{related_context}

---
【記事の構成フォーマット規則】
1. **フロントマター（YAML Frontmatter）を記事先頭に出力してください**:
---
{title_instruction}
summary: "120〜180文字程度の魅力的な記事要約（どの式変形や難解な解説をどう解きほぐしたかを明確に）"
tags:
  - 物理学
  - 数理物理
  - （内容に応じたタグを2〜3個。例: 場の量子論, 量子力学, 解析力学, 統計力学, 相対論など。スラッシュはハイフンに）
---

2. **太字・強調ルールの遵守（最重要）**:
   - テキストを太字にする場合は、Markdownの ** ではなく、必ず HTMLの <strong> タグ（例: <strong>太字テキスト</strong>）を使用してください。

3. **数式ブロックの改行ルール（KaTeX横スクロール対応）**:
   - 独立したブロック数式（$$ ... $$）は、必ず前後に改行を入れて $$ を独立した行に配置してください：
     $$
     数式
     $$

4. **本文の見出し構成**:
   - 本文冒頭に「# タイトル」は置かないでください（Frontmatterから描画されるため）。
   - 見出しは「## （見出し名）」から始めてください。
   - 以下の構成で執筆してください：
     - ## 導入と問題の所在: 何の教科書/論文のどの部分（数式または難解な解説文）が問題なのか、どこが直観に反する・わかりづらいのかを提示。
     - ## 背景にある物理的前提・設定: 座標系、ゲージ、基底、物理的描像の前提を整理。
     - ## 核心の導出 ＆ 概念のかみ砕き解説: （★最重要）
       - 数式変形がある場合: 核心の途中式を KaTeX で省略なしに展開し、変形の根拠を明記。
       - 解説・文章がわかりづらい場合: 著者の主張を解体し、「物理的描像（幾何学的イメージや対称性）」に翻訳して平易かつ深くかみ砕く。
     - ## で、なぜ著者はこのような説明・省略をしたのか？（物理的考察）: （★独自オピニオン）著者の意図や時代背景、数学的必然性や物理的直観を熱量高く語る。
     - ## まとめ ＆ 関連する問い: 総括と、さらなる発展的課題。

【ユーザーからの疑問・着眼点メモ】:
{question_hint if question_hint else "添付の資料（画像・PDF・メモ）から、非自明な式変形や難解な解説・行間を特定し、厳密かつ直観的にわかりやすく解説してください。"}
"""

    prompt_contents = contents + [system_prompt]

    print(f"\n🧠 [Gemini] 物理行間埋め記事を執筆中 (モデル: {model_name})...")
    candidate_models = [
        model_name,
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
    ]

    post_text = None
    for m in candidate_models:
        try:
            response = client.models.generate_content(
                model=m,
                contents=prompt_contents,
            )
            post_text = response.text
            print(f"  ✓ 執筆完了 (モデル: {m})")
            break
        except Exception as e:
            print(f"  ⚠️ {m} でのエラー: {e}")

    if not post_text:
        raise RuntimeError("Gemini による記事生成に失敗しました。")

    return post_text


# ==============================================================================
# 4. yagibrary 向けフォーマット整形 & 保存
# ==============================================================================
def format_and_save_post(
    raw_markdown: str,
    output_dir: str,
    slug_hint: str = "physics-gap",
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Astro 向けに最終フォーマットを整えてファイルに保存"""
    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw_markdown.strip(), re.DOTALL)
    parsed_meta = {}
    body = raw_markdown.strip()

    if frontmatter_match:
        try:
            parsed_meta = yaml.safe_load(frontmatter_match.group(1)) or {}
            body = frontmatter_match.group(2).strip()
        except Exception:
            pass

    # タイトル
    title = parsed_meta.get("title", "物理の行間を埋める導出ノート")
    # 本文冒頭の重複 H1 を削除
    body = re.sub(r"^#\s+.*?\n+", "", body).strip()

    summary = parsed_meta.get("summary", "物理学の教科書・論文における式変形の行間を補完・徹底解説。")
    tags = parsed_meta.get("tags", ["物理学", "数理物理", "式変形"])
    if not isinstance(tags, list):
        tags = ["物理学", "数理物理"]

    cleaned_tags = []
    for t in tags:
        t_clean = str(t).strip().replace('/', '-').replace('\\', '-').replace(':', '-')
        if t_clean and t_clean not in cleaned_tags:
            cleaned_tags.append(t_clean)

    # 日時
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    today_str = datetime.now(jst).strftime("%Y-%m-%d")

    # **太字** を <strong>太字</strong> に変換 (AGENTS.mdルール)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)

    # 数式ブロック $$...$$ の改行正規化
    body = re.sub(r"(?<!\$)\$\$(?!\$)\s*([^\n]+?)\s*\$\$(?!\$)", r"\n\n$$\n\1\n$$\n\n", body)

    # 関連記事リンクセクション
    related_section = ""
    if relevant_posts:
        rel_lines = []
        for rp in relevant_posts:
            s_short = rp.get('summary', '')[:80]
            desc = f" - {s_short}..." if s_short else ""
            rel_lines.append(f"- [{rp['title']}]({rp['url']}){desc}")
        related_section = f"""

---

### 🔗 あわせて読みたい当ブログの関連記事
{chr(10).join(rel_lines)}
"""

    meta_footer = f"""

---

### 📝 物理ノート作成メモ
- 本記事は教科書・論文の行間（非自明な途中計算や物理的仮定）を補完するために Gemini マルチモーダル解析によって生成・整理された導出解説です。
"""

    frontmatter_dict = {
        "title": title,
        "date": now_jst,
        "summary": summary,
        "tags": cleaned_tags,
    }
    frontmatter_yaml = yaml.dump(
        frontmatter_dict,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False
    ).strip()

    final_content = f"---\n{frontmatter_yaml}\n---\n\n{body}{related_section}{meta_footer}\n"

    # ファイル名決定
    # スラッグのサニタイズ
    slug = re.sub(r"[^a-zA-Z0-9_\-]+", "-", slug_hint.lower()).strip("-")
    if not slug:
        slug = "physics-derivation"
    filename = f"{today_str}-{slug}.md"
    file_path = os.path.join(output_dir, filename)

    # 同名ファイルが存在する場合は連番付与
    counter = 1
    base_file_path = file_path
    while os.path.exists(file_path):
        file_path = base_file_path.replace(".md", f"-{counter}.md")
        counter += 1

    os.makedirs(output_dir, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_content)

    return file_path


# ==============================================================================
# メイン CLI エントリポイント
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="物理学の教科書や論文の行間埋め（式変形の導出）をブログ記事化するツール"
    )
    parser.add_argument("--image", type=str, help="スクショ画像ファイルパス (PNG/JPG)")
    parser.add_argument("--clip", action="store_true", help="クリップボードの画像を読み込む")
    parser.add_argument("--pdf", type=str, help="教科書または論文の PDF ファイルパス")
    parser.add_argument("--pages", type=str, help="PDF の対象ページ範囲 (例: '120', '120-123')")
    parser.add_argument(
        "--context", "--md", "--note",
        type=str,
        dest="context",
        help="前後の文脈や手元のメモ (Markdown/Textファイルパス)。--md や --note でも指定可能"
    )
    parser.add_argument("--question", "-q", type=str, help="疑問点や導出したい式の指定（テキスト）")
    parser.add_argument("--title", type=str, help="記事タイトルの希望（未指定時は自動生成）")
    parser.add_argument("--slug", type=str, default="physics-gap", help="ファイル名のスラッグ")
    parser.add_argument("--model", type=str, default="gemini-3.8-flash", help="使用する Gemini モデル")
    parser.add_argument("--output-dir", type=str, default=DEFAULT_YAGIBRARY_POSTS_DIR, help="保存先ディレクトリ")
    parser.add_argument("--dry-run", action="store_true", help="ファイル保存せずコンソールに出力")

    args = parser.parse_args()

    print("\n" + "=" * 65)
    print(" ⚛️  Physics Gap Blogger（物理行間埋めブロガー）起動")
    print("=" * 65)

    gemini_contents = []

    # 1. 画像の処理
    image_bytes = None
    image_label = ""
    if args.clip:
        print("\n📋 クリップボードから画像を取得中...")
        image_bytes = get_clipboard_image_bytes()
        if not image_bytes:
            print("❌ クリップボードに画像が見つかりませんでした。")
            sys.exit(1)
        image_label = "クリップボードの画像"
        print(f"  ✓ クリップボード画像取得成功 ({len(image_bytes):,} bytes)")
    elif args.image:
        resolved_img = resolve_file_path(args.image)
        with open(resolved_img, "rb") as f:
            image_bytes = f.read()
        image_label = f"画像ファイル: {os.path.basename(resolved_img)}"
        print(f"\n🖼️ 画像読込: {resolved_img} ({len(image_bytes):,} bytes)")

    if image_bytes:
        from google.genai import types
        gemini_contents.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/png")
        )

    # 2. PDF の処理
    if args.pdf:
        resolved_pdf = resolve_file_path(args.pdf)
        print(f"\n📑 PDF 読込: {resolved_pdf}")
        pdf_bytes, page_label = extract_pdf_pages_bytes(resolved_pdf, args.pages)
        print(f"  ✓ 抽出範囲: {page_label} ({len(pdf_bytes):,} bytes)")
        from google.genai import types
        gemini_contents.append(
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        )

    # 3. Context (Markdown / Text) の処理
    context_text = ""
    if args.context:
        resolved_ctx = resolve_file_path(args.context)
        print(f"\n📝 コンテキストノート読込: {resolved_ctx}")
        with open(resolved_ctx, "r", encoding="utf-8") as f:
            context_text = f.read()
        gemini_contents.append(
            f"【ユーザーの手元ノート・前後の文脈】\n{context_text}"
        )

    # 4. 疑問点テキスト
    if args.question:
        print(f"\n❓ ユーザーの着眼点/疑問点: {args.question}")
        gemini_contents.append(
            f"【解明したい式変形・行間の疑問】: {args.question}"
        )

    if not gemini_contents:
        print("❌ 入力ソースが指定されていません。--image, --clip, --pdf, --context, --question のいずれかを指定してください。")
        parser.print_help()
        sys.exit(1)

    # 5. 関連記事のインデックス検索
    posts_index = load_existing_posts_index(args.output_dir)
    query_hint = (args.question or "") + " " + context_text[:500]
    relevant_posts = find_relevant_past_posts(query_hint, posts_index, max_matches=3)
    if relevant_posts:
        print(f"\n🔗 関連する過去記事を {len(relevant_posts)} 件検出: {[p['title'][:25] for p in relevant_posts]}")

    # 6. Gemini による生成
    raw_markdown = generate_physics_gap_post(
        contents=gemini_contents,
        question_hint=args.question or "",
        relevant_posts=relevant_posts,
        preferred_title=args.title,
        model_name=args.model,
    )

    # 7. 保存または出力
    if args.dry_run:
        print("\n--- [DRY RUN 出力] ---")
        print(raw_markdown)
        print("--- [DRY RUN 終了] ---")
    else:
        saved_file = format_and_save_post(
            raw_markdown=raw_markdown,
            output_dir=args.output_dir,
            slug_hint=args.slug,
            relevant_posts=relevant_posts,
        )
        print("\n" + "=" * 65)
        print(f" 🎉 ブログ記事の保存が完了しました！")
        print(f" 📄 保存先: {saved_file}")
        print("=" * 65)


if __name__ == "__main__":
    main()
