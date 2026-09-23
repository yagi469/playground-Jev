"""
core/post_formatter.py
======================
yagibrary (Astro) 向けフォーマット整形処理、Frontmatter 生成、
過去記事インデックス・内部リンク生成、Evaluator-Optimizer 推敲レポート付加
"""

import os
import re
import yaml
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from config import DEFAULT_YAGIBRARY_POSTS_DIR
from core.figure_extractor import embed_figures_in_markdown


def get_existing_arxiv_ids(posts_dir: str) -> set:
    """
    保存先ディレクトリ内の既存記事から、既に執筆済みの arXiv ID を収集。
    """
    existing_ids = set()
    if not os.path.exists(posts_dir):
        return existing_ids

    for fname in os.listdir(posts_dir):
        if not fname.endswith(".md"):
            continue

        m = re.search(r"arxiv-([a-zA-Z0-9_\-]+)\.md$", fname)
        if m:
            clean_id = m.group(1).lower()
            existing_ids.add(clean_id)
            base_clean = re.sub(r"v\d+$", "", clean_id)
            existing_ids.add(base_clean)
            existing_ids.add(clean_id.replace('-', '.'))
            existing_ids.add(base_clean.replace('-', '.'))

        filepath = os.path.join(posts_dir, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                header = "".join([f.readline() for _ in range(70)])
                found = re.findall(r"arxiv(?:\.org/(?:abs|pdf)/|:?\s*)([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", header, re.IGNORECASE)
                for fid in found:
                    fid_clean = fid.lower().replace('/', '_').replace('.', '-')
                    existing_ids.add(fid.lower())
                    existing_ids.add(fid_clean)
                    existing_ids.add(re.sub(r"v\d+$", "", fid.lower()))
                    existing_ids.add(re.sub(r"v\d+$", "", fid_clean))
        except Exception:
            pass

    return existing_ids


def is_paper_already_blogged(paper: Dict[str, Any], existing_ids: set) -> bool:
    """論文が既に記事化されているかチェック"""
    raw_id = paper.get("arxiv_id", "").lower()
    clean_id = raw_id.replace('/', '_').replace('.', '-')
    base_id = re.sub(r"v\d+$", "", raw_id)
    base_clean = re.sub(r"v\d+$", "", clean_id)

    candidates = {raw_id, clean_id, base_id, base_clean}
    return any(c in existing_ids for c in candidates)


def load_existing_posts_index(posts_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """保存先ディレクトリ内の既存記事からインデックスを構築"""
    if posts_dir is None:
        posts_dir = DEFAULT_YAGIBRARY_POSTS_DIR

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
    current_title: str,
    current_tags: List[str],
    current_text: str,
    posts_index: List[Dict[str, Any]],
    current_slug: Optional[str] = None,
    max_matches: int = 3,
) -> List[Dict[str, Any]]:
    """現在の記事から関連度の高い過去記事を選出"""
    if not posts_index:
        return []

    scored_posts = []
    current_tag_set = {str(t).lower() for t in current_tags}

    arxiv_id_match = re.search(r"(\d{4}[\.-]\d{4,5})", current_title + " " + (current_slug or ""))
    current_arxiv_core = arxiv_id_match.group(1).replace(".", "-") if arxiv_id_match else None

    stopwords = {
        "the", "and", "for", "with", "this", "that", "from", "into", "over", "under", "about",
        "解説", "入門", "理論", "物理学", "記事", "概要", "まとめ", "徹底", "考察",
        "2024", "2025", "2026", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12",
        "part", "vol", "chapter", "第1章", "第2章", "前編", "後編"
    }

    current_keywords = {
        w for w in re.findall(r"[\w]+", (current_title + " " + current_text[:1000]).lower())
        if len(w) >= 2 and w not in stopwords
    }

    for post in posts_index:
        if current_slug and post["slug"] == current_slug:
            continue

        score = 0
        post_slug = post.get("slug", "")
        post_title = post.get("title", "")
        post_summary = post.get("summary", "")
        post_tags = {str(t).lower() for t in post.get("tags", [])}

        if current_arxiv_core and current_arxiv_core in post_slug.replace(".", "-"):
            score += 50

        common_tags = current_tag_set & post_tags
        score += len(common_tags) * 10

        post_keywords = {
            w for w in re.findall(r"[\w]+", (post_title + " " + post_summary).lower())
            if len(w) >= 2 and w not in stopwords
        }
        common_words = current_keywords & post_keywords
        score += len(common_words) * 2

        if len(common_tags) == 0 and len(common_words) < 3:
            continue

        physics_tags = {"物理学", "場の量子論", "素粒子論", "超弦理論", "数理物理", "数理物理学", "有効場の理論", "双対性", "入門解説", "トポロジカル場論", "量子情報"}
        business_cloud_tags = {"週4時間だけ働く", "ライフスタイル設計", "キャリア", "働き方", "書評", "aws", "インフラ", "セキュリティ", "cloudfront"}

        is_current_physics = bool(current_tag_set & {t.lower() for t in physics_tags})
        is_post_business_cloud = bool(post_tags & {t.lower() for t in business_cloud_tags})
        if is_current_physics and is_post_business_cloud:
            continue

        if score > 0:
            scored_posts.append((score, post))

    scored_posts.sort(key=lambda x: x[0], reverse=True)
    return [p[1] for p in scored_posts[:max_matches]]


def format_post_for_yagibrary(
    raw_markdown: str,
    paper: Dict[str, Any],
    quality: Dict[str, Any],
    rank: int = 1,
    time_offset_seconds: int = 0,
    history: Optional[List[Dict[str, Any]]] = None,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    figures: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """yagibrary (Astro) の形式に合わせて論文記事を整形"""
    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw_markdown.strip(), re.DOTALL)
    parsed_meta = {}
    body = raw_markdown.strip()

    if frontmatter_match:
        yaml_content = frontmatter_match.group(1)
        body = frontmatter_match.group(2).strip()
        try:
            parsed_meta = yaml.safe_load(yaml_content) or {}
        except Exception as e:
            print(f"⚠️ Frontmatter YAML パース失敗: {e}")

    title = parsed_meta.get("title")
    if not title:
        h1_match = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
        if h1_match:
            title = h1_match.group(1).strip()
            body = re.sub(r"^#\s+.+\n*", "", body, count=1).strip()
        else:
            title = f"【arXiv最新論文解説】{paper['title']}"

    body = re.sub(r"^#\s+.*?\n+", "", body).strip()

    summary = parsed_meta.get("summary")
    if not summary:
        summary = f"arXiv:{paper['arxiv_id']} 「{paper['title']}」の徹底解説。量子情報・物理の最新進展と筆者独自のオピニオンを交えて紐解きます。"

    tags = parsed_meta.get("tags")
    if not tags or not isinstance(tags, list):
        tags = ["物理学", "量子情報", paper["jev_metrics"].get("subfield", "理論物理")]

    cleaned_tags = []
    for t in tags:
        t_clean = str(t).strip().replace('/', '-').replace('\\', '-').replace(':', '-')
        if t_clean and t_clean not in cleaned_tags:
            cleaned_tags.append(t_clean)

    jst = timezone(timedelta(hours=9))
    post_time = datetime.now(jst) + timedelta(seconds=time_offset_seconds)
    now_jst = post_time.strftime("%Y-%m-%dT%H:%M:%S+09:00")

    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"(?<!\$)\$\$(?!\$)\s*([^\n]+?)\s*\$\$(?!\$)", r"\n\n$$\n\1\n$$\n\n", body)

    figs_to_embed = figures or paper.get("figures")
    body = embed_figures_in_markdown(body, figs_to_embed)

    related_section = ""
    if relevant_posts:
        rel_lines = []
        for rp in relevant_posts:
            s_short = rp.get('summary', '')[:90]
            desc = f" - {s_short}..." if s_short else ""
            rel_lines.append(f"- [{rp['title']}]({rp['url']}){desc}")
        related_section = f"""

---

### 🔗 あわせて読みたい当ブログの関連記事
{chr(10).join(rel_lines)}
"""

    revision_count = len(history) if history else 1
    history_steps = []
    if history:
        for h in history:
            round_lbl = f"第{h.get('round', 1)}稿"
            score_lbl = f"{h.get('total_score', 0):.2f}点"
            status_lbl = "合格" if h.get("passed") else f"足切り ({h.get('diagnosis', '要改善')})"
            history_steps.append(f"{round_lbl}: {score_lbl} [{status_lbl}]")
    history_summary = " ➡️ ".join(history_steps) if history_steps else f"{quality.get('total_score', 0):.2f}点"

    meta_section = f"""

---

### 📊 本日の自律型 AI パイプライン採点レポート
- <strong>選定元</strong>: [<a href="{paper['url']}" target="_blank" rel="noopener noreferrer">arXiv:{paper['arxiv_id']}</a>] / カテゴリ: {', '.join(paper['categories'])}
- <strong>本日のランキング</strong>: 第{rank}位（選考スコア: <code>{paper['jev_metrics']['total_score']}</code>）
- <strong>Jev 論文スクリーニング</strong>:
  - 数理物理核心度: <code>{paper['jev_metrics'].get('is_math_physics_core', paper['jev_metrics'].get('is_quantum_relevant', 0.0))*100:.1f}%</code>
  - 理論的新規性・深度: <code>{paper['jev_metrics']['theoretical_depth']:.2f} / 3.0</code>
  - 話題性・アピール度: <code>{paper['jev_metrics']['blog_appeal']:.2f} / 3.0</code>
  - サブ領域: <code>{paper['jev_metrics']['subfield']}</code>
- <strong>Jev 記事品質推敲（Evaluator-Optimizer）</strong>:
  - 最終品質スコア: <code>{quality.get('total_score', 0):.2f} / 12.0</code>（判定: <code>{'合格' if quality.get('passed') else '足切り後採用'}</code>）
  - 数理・理論の具体性: <code>{quality.get('math_depth', 0):.2f} / 3.0</code>
  - 行間・途中計算の丁寧さ: <code>{quality.get('clarity', 0):.2f} / 3.0</code>
  - 筆者オピニオン度: <code>{quality.get('stance', 0):.2f} / 3.0</code>
  - 知的好奇心刺激度: <code>{quality.get('appeal', 0):.2f} / 3.0</code>
  - 「で、あなたの意見は？」リスク: <code>{quality.get('lack_of_opinion_risk', 0)*100:.1f}%</code>
  - 「難解・置いてけぼり」リスク: <code>{quality.get('rushed_math_risk', 0)*100:.1f}%</code>
  - 自律推敲・改善プロセス (計 {revision_count} 回): <code>{history_summary}</code>
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

    return f"---\n{frontmatter_yaml}\n---\n\n{body}{related_section}\n{meta_section}"


def format_doc_post_for_yagibrary(
    raw_markdown: str,
    doc_info: Dict[str, Any],
    quality: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    figures: Optional[List[Dict[str, Any]]] = None,
    extra_tags: Optional[List[str]] = None,
) -> str:
    """yagibrary (Astro) の形式に合わせてドキュメント記事を整形"""
    clean_md = raw_markdown.strip()
    if clean_md.startswith("```"):
        clean_md = re.sub(r"^```(?:markdown)?\s*\n", "", clean_md)
        clean_md = re.sub(r"\n```\s*$", "", clean_md).strip()

    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", clean_md, re.DOTALL)
    parsed_meta = {}
    body = clean_md

    if frontmatter_match:
        yaml_content = frontmatter_match.group(1)
        body = frontmatter_match.group(2).strip()
        try:
            parsed_meta = yaml.safe_load(yaml_content) or {}
        except Exception as e:
            print(f"⚠️ Frontmatter YAML パース失敗: {e}")

    title = parsed_meta.get("title")
    if not title:
        h1_match = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
        if h1_match:
            title = h1_match.group(1).strip()
            body = re.sub(r"^#\s+.+\n*", "", body, count=1).strip()
        else:
            title = f"【文献解説】{doc_info['title']}"

    body = re.sub(r"^#\s+.*?\n+", "", body).strip()

    genre = doc_info.get("genre", "general")
    summary = parsed_meta.get("summary")
    if not summary:
        if genre in ["business", "management"]:
            summary = f"『{doc_info['title']}』の解説記事。組織マネジメントの本質と実践フレームワークを独自のオピニオンを交えて紐解きます。"
        elif genre in ["tech", "engineering"]:
            summary = f"『{doc_info['title']}』の技術解説。アーキテクチャの核心とトレードオフを独自の視点で紐解きます。"
        elif genre in ["physics", "math"]:
            summary = f"『{doc_info['title']}』の解説記事。数理物理の深層と独自のオピニオンを交えて紐解きます。"
        elif genre in ["classics", "history", "humanities"]:
            summary = f"『{doc_info['title']}』の教養解説。古典の核心ドラマと現代への普遍的教訓を独自のオピニオンを交えて紐解きます。"
        else:
            summary = f"『{doc_info['title']}』の解説記事。核心の洞察と独自のオピニオンを交えて紐解きます。"

    tags = parsed_meta.get("tags")
    if not tags or not isinstance(tags, list):
        if genre in ["business", "management"]:
            tags = ["ビジネス", "マネジメント", "組織論", doc_info.get("subfield", "生産性")]
        elif genre in ["tech", "engineering"]:
            tags = ["エンジニアリング", "テクノロジー", "アーキテクチャ"]
        elif genre in ["physics", "math"]:
            tags = ["物理学", "数理物理", doc_info.get("subfield", "理論物理")]
        elif genre in ["classics", "history", "humanities"]:
            tags = ["古典", "歴史", "教養", "読書論考", "リーダーシップ"]
        else:
            tags = ["読書論考", "文献解説", "教養"]

    cleaned_tags = []
    if extra_tags and isinstance(extra_tags, list):
        for et in extra_tags:
            et_clean = str(et).strip().replace('/', '-').replace('\\', '-').replace(':', '-')
            if et_clean and et_clean not in cleaned_tags:
                cleaned_tags.append(et_clean)

    for t in tags:
        t_clean = str(t).strip().replace('/', '-').replace('\\', '-').replace(':', '-')
        if t_clean and t_clean not in cleaned_tags:
            cleaned_tags.append(t_clean)

    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"(?<!\$)\$\$(?!\$)\s*([^\n]+?)\s*\$\$(?!\$)", r"\n\n$$\n\1\n$$\n\n", body)

    figs_to_embed = figures or doc_info.get("figures")
    body = embed_figures_in_markdown(body, figs_to_embed)

    related_section = ""
    if relevant_posts:
        rel_lines = []
        for rp in relevant_posts:
            s_short = rp.get('summary', '')[:90]
            desc = f" - {s_short}..." if s_short else ""
            rel_lines.append(f"- [{rp['title']}]({rp['url']}){desc}")
        related_section = f"""

---

### 🔗 あわせて読みたい当ブログの関連記事
{chr(10).join(rel_lines)}
"""

    revision_count = len(history) if history else 1
    history_steps = []
    if history:
        for h in history:
            round_lbl = f"第{h.get('round', 1)}稿"
            score_lbl = f"{h.get('total_score', 0):.2f}点"
            if h.get("passed"):
                status_lbl = "合格 (最高品質)" if h.get("diagnosis") == "high_quality" else "合格"
            else:
                diag = h.get('diagnosis', '要改善')
                status_lbl = "推敲継続" if diag == "high_quality" else f"要改善 ({diag})"
            history_steps.append(f"{round_lbl}: {score_lbl} [{status_lbl}]")
    history_summary = " ➡️ ".join(history_steps) if history_steps else f"{quality.get('total_score', 0):.2f}点"

    final_pass_str = (
        "合格 (最高品質クリア)" if quality.get("passed") and quality.get("diagnosis") == "high_quality"
        else ("合格" if quality.get("passed")
        else ("高評価採用" if quality.get("total_score", 0) >= 10.0
        else "足切り後採用"))
    )

    chapter_info = f"- <strong>対象章・セクション</strong>: {doc_info['chapter_hint']}\n" if doc_info.get("chapter_hint") else ""
    if genre in ["physics", "math"]:
        depth_label = "数理・理論の具体性"
    elif genre in ["tech", "engineering"]:
        depth_label = "技術・アーキテクチャの具体性"
    elif genre in ["classics", "history", "humanities"]:
        depth_label = "人間ドラマ・知略の具体性"
    else:
        depth_label = "モデル・因果関係の具体性"

    meta_section = f"""

---

### 📊 本日の自律型 AI ドキュメント解析レポート
- <strong>解析対象</strong>: <code>{doc_info['file_name']}</code> ({doc_info.get('page_label', '全編')})
- <strong>判定ジャンル</strong>: <code>{genre}</code>
{chapter_info}- <strong>Jev 記事品質推敲（Evaluator-Optimizer）</strong>:
  - 最終品質スコア: <code>{quality.get('total_score', 0):.2f} / 12.0</code>（判定: <code>{final_pass_str}</code>）
  - {depth_label}: <code>{quality.get('math_depth', 0):.2f} / 3.0</code>
  - 構成の明快さ: <code>{quality.get('clarity', 0):.2f} / 3.0</code>
  - 筆者オピニオン度: <code>{quality.get('stance', 0):.2f} / 3.0</code>
  - 知的好奇心刺激度: <code>{quality.get('appeal', 0):.2f} / 3.0</code>
  - 「で、あなたの意見は？」リスク: <code>{quality.get('lack_of_opinion_risk', 0)*100:.1f}%</code>
  - 自律推敲・改善プロセス (計 {revision_count} 回): <code>{history_summary}</code>
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

    return f"---\n{frontmatter_yaml}\n---\n\n{body}{related_section}\n{meta_section}"
