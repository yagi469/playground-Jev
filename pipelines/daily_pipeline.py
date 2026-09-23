"""
pipelines/daily_pipeline.py
===========================
arXiv の日次自動スクリーニング＆解説記事執筆パイプライン
"""

import os
from datetime import datetime
from typing import List, Optional

from config import DEFAULT_YAGIBRARY_POSTS_DIR
from core.arxiv_client import fetch_arxiv_papers, fetch_arxiv_paper_content
from core.figure_extractor import fetch_arxiv_paper_figures
from core.post_formatter import (
    load_existing_posts_index,
    get_existing_arxiv_ids,
    is_paper_already_blogged,
    find_relevant_past_posts,
    format_post_for_yagibrary,
)
from generators.paper_generator import (
    screen_and_rank_papers_with_jev,
    generate_refined_blog_post,
)


def run_daily_pipeline(
    max_papers: int = 50,
    top_n_to_blog: int = 3,
    output_dir: Optional[str] = None
) -> List[str]:
    """arXiv 日次論文自動スクリーニング＆ブログ執筆"""
    print("\n" + "=" * 65)
    print(" 🚀 arXiv × TypeSafe Jev × Gemini 全自動論文ブロガー 起動")
    print(" 🎯 対象カテゴリ: hep-th (高エネルギー理論) & quant-ph (量子情報)")
    print(f" 📑 記事生成対象: 上位 {top_n_to_blog} 件")
    print("=" * 65)

    if output_dir is None:
        if os.path.exists(DEFAULT_YAGIBRARY_POSTS_DIR):
            target_dir = DEFAULT_YAGIBRARY_POSTS_DIR
        else:
            target_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "generated_posts")
    else:
        target_dir = output_dir

    papers = fetch_arxiv_papers(max_results=max_papers)
    if not papers:
        print("❌ 論文を取得できませんでした。終了します。")
        return []

    ranked_papers = screen_and_rank_papers_with_jev(papers)
    if not ranked_papers:
        print("❌ スクリーニングに失敗しました。")
        return []

    display_count = min(len(ranked_papers), max(3, top_n_to_blog))
    print(f"\n🏆 【本日の TOP {display_count} 厳選論文】")
    for i, p in enumerate(ranked_papers[:display_count]):
        m = p["jev_metrics"]
        print(f"  第{i+1}位: [{p['arxiv_id']}] {p['title'][:65]}...")
        print(f"         総合スコア: {m['total_score']} | 関連度: {m.get('is_math_physics_core', m.get('is_quantum_relevant', 0.0)):.1%} | 魅力: {m['blog_appeal']:.1f} | {m['subfield']}")

    posts_index = load_existing_posts_index(target_dir)
    existing_arxiv_ids = get_existing_arxiv_ids(target_dir)
    if existing_arxiv_ids:
        print(f"📚 既存記事ディレクトリ ({target_dir}) から {len(posts_index)} 件の記事インデックスと執筆済み arXiv ID を照合中...")

    target_papers = []
    for rank_idx, paper in enumerate(ranked_papers):
        overall_rank = rank_idx + 1
        paper['overall_rank'] = overall_rank
        if is_paper_already_blogged(paper, existing_arxiv_ids):
            print(f"  ⏩ [第{overall_rank}位: {paper['arxiv_id']}] は既に記事が存在するためスキップ")
            continue
        target_papers.append(paper)
        if len(target_papers) >= top_n_to_blog:
            break

    if not target_papers:
        print("\n✨ 取得した上位論文はすべて執筆済みです。新規生成をスキップして終了します。")
        return []

    print(f"\n📝 未執筆の論文 {len(target_papers)} 件を順次ブログ記事化します...")

    os.makedirs(target_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    generated_files = []

    for i, paper in enumerate(target_papers):
        rank = paper.get('overall_rank', i + 1)
        batch_idx = i + 1
        print("\n" + "-" * 65)
        print(f" 🖋️ [記事執筆・整形 {batch_idx}/{len(target_papers)}] 総合第{rank}位: {paper['arxiv_id']}")
        print(f"    タイトル: {paper['title']}")
        print("-" * 65)

        paper_tags = paper.get("categories", []) + [paper.get("jev_metrics", {}).get("subfield", "")]
        relevant_posts = find_relevant_past_posts(
            current_title=paper.get("title", ""),
            current_tags=paper_tags,
            current_text=paper.get("summary", ""),
            posts_index=posts_index,
            max_matches=3,
        )
        if relevant_posts:
            print(f"  🔗 関連する過去記事を {len(relevant_posts)} 件検出: {[p['title'][:30] for p in relevant_posts]}")

        paper["full_text_content"] = fetch_arxiv_paper_content(paper["arxiv_id"])
        paper["figures"] = fetch_arxiv_paper_figures(paper)

        raw_markdown, quality, history = generate_refined_blog_post(
            paper=paper,
            rank=rank,
            max_revisions=2,
            relevant_posts=relevant_posts,
        )

        time_offset = (len(target_papers) - batch_idx) * 60
        final_post = format_post_for_yagibrary(
            raw_markdown,
            paper,
            quality,
            rank=rank,
            time_offset_seconds=time_offset,
            history=history,
            relevant_posts=relevant_posts,
            figures=paper.get("figures"),
        )

        clean_id = paper['arxiv_id'].replace('/', '_').replace('.', '-')
        filename = f"arxiv-{clean_id}.md"
        file_path = os.path.join(target_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_post)

        generated_files.append(file_path)
        print(f" ✅ [第{rank}位] 記事保存完了: {file_path}")

    print("\n" + "=" * 65)
    print(f" 🎉 全 {len(generated_files)} 件のブログ記事の自動生成が完了しました！")
    for idx, fp in enumerate(generated_files):
        print(f"   第{idx+1}位: {fp}")
    print("=" * 65)
    return generated_files
