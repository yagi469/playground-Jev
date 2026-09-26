"""
generators/paper_generator.py
=============================
arXiv論文向けの Gemini 執筆、Jev スクリーニング、Evaluator-Optimizer リライト推敲ループ
"""

import os
import json
from typing import List, Dict, Any, Optional, Tuple
from typesafe_sdk import Score, Noul
from config import init_gemini_client, get_typesafe_client
from core.figure_extractor import build_figures_prompt_components
from generators.prompts import get_genre_blog_config
from score_post import verify_post_with_jev


def resolve_paper_genre(paper: Dict[str, Any], explicit_genre: Optional[str] = "auto") -> str:
    """論文のカテゴリやタイトルから適切なジャンルを決定"""
    if explicit_genre and explicit_genre != "auto":
        return explicit_genre

    categories = [c.lower() for c in paper.get("categories", [])]
    title_lower = paper.get("title", "").lower()
    summary_lower = paper.get("summary", "").lower()

    # 1. 統計学・計量経済学・因果推論
    if any(c.startswith("econ.") or c.startswith("stat.") or c.startswith("q-fin.") for c in categories):
        return "stats"
    if any(k in title_lower or k in summary_lower for k in [
        "econometrics", "causal inference", "regression discontinuity", "treatment effect", "potential outcomes", "instrumental variable"
    ]):
        return "stats"

    # 2. コンピュータ科学・ソフトウェア
    if any(c.startswith("cs.") for c in categories):
        return "tech"

    # 3. 物理学（デフォルト）
    return "physics"


def load_user_interests() -> Dict[str, Any]:
    """Google Driveの文献等から抽出されたユーザー興味プロファイルをロード"""
    profile_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "user_interests.json")
    if os.path.exists(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ user_interests.json の読み込み失敗: {e}")
    return {}


def screen_and_rank_papers_with_jev(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    各論文を Jev (System One) で並列採点し、
    Google Driveの読書傾向に合致し、価値が高く面白い論文をランキング
    """
    client = get_typesafe_client()
    user_profile = load_user_interests()
    user_criteria = user_profile.get("evaluation_criteria_for_jev", "")
    core_themes = ", ".join(user_profile.get("core_themes", []))

    print(f"\n⚡ [TypeSafe Jev] {len(papers)} 件の論文をユーザー興味プロファイルに基づいて多面スクリーニング中...")
    if core_themes:
        print(f"🎯 反映中の興味テーマ: {core_themes[:60]}...")

    scored_papers = []

    questions = {
        "is_math_physics_core": Noul(
            instructions=(
                "この論文は、場の量子論の厳密な数理構造、超対称共形場理論（SCFT）、"
                "カイラル代数/頂点作用素代数 (VOA)、Dブレーン・超弦理論の幾何学、"
                "トポロジカル場の量子論 (TQFT)、共形ブートストラップ、"
                "あるいはAdS/CFT対応の厳密な数理・代数的側面に明確に関連していますか？"
            )
        ),
        "user_interest_match": Score(
            instructions=(
                f"ユーザーの興味基準に基づき、この論文がユーザーの研究的関心にどれだけマッチするか評価してください。\n"
                f"【ユーザー関心基準】: {user_criteria if user_criteria else '超対称共形場理論、カイラル代数、4d-2d対応、Dブレーン幾何、TQFT、数理物理'}"
            ),
            criteria=[
                "全く関心外（素粒子実験フィッティング、単なる量子回路実装、物性模型の数値計算など）",
                "やや関連（一般的な量子情報応用、ブラックホール熱力学の初歩的計算など）",
                "強くマッチ（AdS/CFTの厳密幾何、高次対称性・TQFT、共形ブートストラップ、量子エンタングルメントの厳密代数）",
                "ドンピシャ（超対称場論、SCFT、4d-2d対応、カイラル代数/頂点代数、Dブレーン幾何、BPS不変量、厳密解法）",
            ],
        ),
        "theoretical_depth": Score(
            instructions="この論文の理論的深さや数学・物理学的な新規性・厳密性レベルを評価してください",
            criteria=[
                "初歩的・既存のレビューや軽微な計算",
                "標準的な応用や既存枠組み内の進展",
                "独自の新たな発見・非自明な理論的ブレイクスルーがある",
                "極めて重要な金字塔・パラダイムシフトの可能性を秘める",
            ],
        ),
        "blog_appeal": Score(
            instructions="ブログ読者（理論物理・量子情報に関心を持つ層）へのアピール度や知的好奇心を刺激する話題性",
            criteria=[
                "専門的すぎて一般の理論物理ファンには退屈",
                "標準的な興味を惹く内容",
                "キャッチーで論点が明確、知的好奇心を刺激する",
                "極めてエキサイティング、誰もが読みたくなる話題",
            ],
        ),
    }

    for paper in papers:
        summary_text = (
            f"Title: {paper['title']}\n"
            f"Categories: {', '.join(paper['categories'])}\n"
            f"Abstract: {paper['summary']}"
        )
        try:
            result = client.system_one(
                state={"paper": summary_text},
                questions=questions,
            )
            is_core = result.nouls["is_math_physics_core"].noul
            match_score = result.scores["user_interest_match"].score
            depth_score = result.scores["theoretical_depth"].score
            appeal_score = result.scores["blog_appeal"].score

            # 総合選定スコア
            total_score = (is_core * 3.5) + (match_score * 2.5) + (depth_score * 2.0) + (appeal_score * 2.0)

            paper_copy = dict(paper)
            paper_copy["jev_metrics"] = {
                "total_score": round(total_score, 2),
                "is_math_physics_core": round(is_core, 2),
                "user_interest_match": round(match_score, 2),
                "theoretical_depth": round(depth_score, 2),
                "blog_appeal": round(appeal_score, 2),
                "subfield": "数理物理・理論物理",
            }
            scored_papers.append(paper_copy)
        except Exception as e:
            print(f"  ⚠️ Jev 採点エラー ({paper['arxiv_id']}): {e}")

    scored_papers.sort(key=lambda x: x["jev_metrics"]["total_score"], reverse=True)
    return scored_papers


def write_blog_post_with_gemini(
    paper: Dict[str, Any],
    rank: int = 1,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    genre: Optional[str] = "auto",
) -> str:
    """選定されたベスト論文をもとに、ブログ記事を自動執筆"""
    client = init_gemini_client()

    m = paper["jev_metrics"]
    resolved_genre = resolve_paper_genre(paper, genre)
    genre_cfg = get_genre_blog_config(resolved_genre, {"title": paper["title"], "subfield": m.get("subfield", "")})

    print(f"\n🧠 [Gemini] 総合第{rank}位の論文 {paper['arxiv_id']} のブログ記事を執筆中...")
    print(f"   タイトル: {paper['title']}")
    print(f"   ジャンル: {resolved_genre} (総合スコア: {m['total_score']})")

    user_perspective = ""
    if resolved_genre == "physics":
        user_profile = load_user_interests()
        if user_profile:
            themes = ", ".join(user_profile.get("core_themes", []))
            user_perspective = f"\n【筆者の専門的バックボーン・着眼点（Google Driveの蔵書・関心より）】\n- 筆者は場の量子論、超対称共形場理論（SCFT）、カイラル代数、Dブレーン幾何、トポロジカル場論（TQFT）、AdS/CFT対応などの数理的側面に強い思い入れがあります。\n- 関心テーマ: {themes}\n- 「で、私（筆者）はどう考えるか？」のセクションでは、これらの数理物理観点も交えつつ、独自の一歩踏み込んだ深いオピニオンを熱量高く語ってください。\n"
    elif resolved_genre in ["stats", "econometrics"]:
        user_perspective = (
            "\n【筆者の専門的バックボーン・着眼点】\n"
            "- 筆者はデータサイエンス、計量経済学、統計的因果推論、ノンパラメトリック推定、機械学習の数理に強い関心があります。\n"
            "- 「で、私（筆者）はどう考えるか？」では、実証分析への影響、他手法との比較、実務での適用限界や仮定の妥当性について、骨太なオピニオンを展開してください。\n"
        )

    related_context = ""
    if relevant_posts:
        lines = []
        for rp in relevant_posts:
            lines.append(f"- [{rp['title']}]({rp['url']}) (概要: {rp.get('summary', '')[:100]})")
        related_context = f"\n【当ブログの関連する過去記事（文脈の記憶）】\n本文の論理展開の中で、関連する概念や背景に触れる際、自然に以下の過去記事への言及・リンクを1〜2箇所織り交ぜてください：\n" + "\n".join(lines) + "\n"

    full_text_section = ""
    fc = paper.get("full_text_content")
    if fc:
        if fc.get("pages_text"):
            full_text_section = f"""
【論文の指定分析対象ページ ({paper.get('focus_pages', '')}) からのテキスト・数式抜粋】
{fc.get('pages_text')[:15000]}
"""
        else:
            full_text_section = f"""
【論文本文（HTML）からの重要抜粋】
- 論文のセクション構成: {fc.get('section_names', 'N/A')}
- 序論・動機（Introduction）:
{fc.get('intro', '')[:2500]}
- 核心となる定理・モデル・計算（Main Results / Setup）:
{fc.get('main_results', '')[:3500]}
- 結論・展望（Conclusion / Outlook）:
{fc.get('conclusion', '')[:1500]}
"""

    figures_instruction, fig_parts = build_figures_prompt_components(paper.get("figures"))

    prompt = f"""{genre_cfg["persona"]}
読者が「で、あなたの意見は？」と突っ込みたくなるような退屈なAIまとめ記事ではなく、
安易な表面論に逃げず、本論文が提起する核心アイデア・数理的機構・実務や理論へのインパクトを生き生きと語り尽くす、知的好奇心を刺激する熱いブログ記事を執筆してください。
{user_perspective}{related_context}{figures_instruction}
【取り上げる論文情報】
- arXiv ID: {paper['arxiv_id']}
- 論文タイトル: {paper['title']}
- 著者: {', '.join(paper['authors'])}
- カテゴリ: {', '.join(paper['categories'])}
- 論文URL: {paper['url']}
- PDFリンク: {paper['pdf_url']}
- アブストラクト (英文):
{paper['summary']}
{full_text_section}
【分析評価】
- 専門領域: {m.get('subfield', resolved_genre)}

---
【記事の構成とフォーマット規則】
1. **フロントマター（YAML Frontmatter）を記事先頭に必ず出力してください**:
---
title: "思わずクリックしたくなる、知的好奇心と学術的本質を突いた日本語タイトル"
summary: "120〜180文字程度の魅力的な記事要約"
tags:
  - {genre_cfg['default_tags'][0]}
  - （論文内容に即したタグを3〜5個。スラッシュは使わずハイフンを使用）
---

2. **太字・強調ルールの遵守（最重要）**:
   - テキストを太字・強調する場合は、必ず HTMLの <strong> タグ（例: <strong>太字テキスト</strong>）を使用してください。Markdownの ** は禁止です。

3. **本文の見出し構成**:
   - 本文の開始部分に「# タイトル」を置かないでください。
{genre_cfg["sections"]}

4. **執筆ルール・数式**:
{genre_cfg["guidance"]}
- 数式ブロック（$$ ... $$）は、必ず前後に改行を入れて独立行で出力。
- 記号の定義や前提知識を丁寧に導入し、途中計算の行間を明示。
- 記事末尾には必ず対象論文へのリンク（[{paper['arxiv_id']}]({paper['url']})）およびPDFリンク（[PDF]({paper['pdf_url']})）を掲載。

Markdown形式で出力してください。
"""

    prompt_contents = fig_parts + [prompt]
    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash",
    ]
    post_text = None
    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt_contents,
            )
            post_text = response.text
            print(f"  ✓ Gemini 執筆完了 (モデル: {model_name})")
            break
        except Exception as e:
            print(f"  ⚠️ {model_name} でのエラー: {e}")

    if not post_text:
        raise RuntimeError("Gemini による執筆に失敗しました。")

    return post_text


def rewrite_blog_post_with_gemini(
    paper: Dict[str, Any],
    previous_draft: str,
    feedback_metrics: Dict[str, Any],
    revision_round: int,
    rank: int = 1,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    genre: Optional[str] = "auto",
) -> str:
    """Jev による診断結果・ボトルネック指摘に基づき、Gemini にブログ記事をリライトさせる"""
    client = init_gemini_client()
    resolved_genre = resolve_paper_genre(paper, genre)
    genre_cfg = get_genre_blog_config(resolved_genre, {"title": paper["title"]})

    print(f"\n🔄 [Gemini リライト Round {revision_round}] Jevの改善フィードバックを反映して記事を再推敲中 (ジャンル: {resolved_genre})...")

    diagnosis = feedback_metrics.get("diagnosis", "")
    focus_instructions = []

    if feedback_metrics.get("clarity", 0.0) < 2.0 or feedback_metrics.get("rushed_math_risk", 0.0) >= 0.35 or diagnosis == "need_pedagogical_steps":
        focus_instructions.append(
            "- 【最重要：前提知識と途中計算（行間）の徹底解説】難解な専門用語や結果の数式をいきなり展示するのをやめ、"
            "記号の定義、途中計算のステップ（1〜3ステップ）を本文中に明記してください。"
        )

    if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
        focus_instructions.append(
            "- 【最重要：数理的機構の具体化】具体的な数学的・理論的機構（識別条件、推定量、仮定の妥当性など）が"
            "論理的にどう機能しているのかを明快に解説してください。"
        )

    if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
        focus_instructions.append(
            "- 【最重要：オピニオンの徹底強化】「で、私（筆者）はどう考えるか？」セクションで、"
            "明確なスタンスと鋭い批評を展開してください。"
        )

    if diagnosis == "avoid_shallow_metaphors":
        focus_instructions.append(
            "- 【比喩の排除・主題への集中】安易なたとえ話や無関係な分野からの無理なこじつけを排除し、論文本来の学術的本質に徹してください。"
        )

    if not focus_instructions:
        focus_instructions.append(
            "- 前回のドラフト全体の論理のつながり、数理的説明のキレ、筆者の独自スタンスをさらに研ぎ澄ましてください。"
        )

    focus_text = "\n".join(focus_instructions)

    full_text_section = ""
    fc = paper.get("full_text_content")
    if fc:
        if fc.get("pages_text"):
            full_text_section = f"""
【論文の指定分析対象ページ ({paper.get('focus_pages', '')}) からのテキスト・数式抜粋】
{fc.get('pages_text')[:15000]}
"""
        else:
            full_text_section = f"""
【論文本文（HTML）からの重要抜粋】
- 論文のセクション構成: {fc.get('section_names', 'N/A')}
- 序論・動機（Introduction）: {fc.get('intro', '')[:2500]}
- 核心となる定理・モデル・計算（Main Results / Setup）: {fc.get('main_results', '')[:3500]}
- 結論・展望（Conclusion / Outlook）: {fc.get('conclusion', '')[:1500]}
"""

    rewrite_prompt = f"""{genre_cfg['persona']}
先ほど執筆したブログ記事ドラフトに対し、Jev System One 診断システムから以下の改善要求が届きました。

【Jev による前稿（第{revision_round - 1}稿）の診断結果】
- 数理・理論の具体性スコア: {feedback_metrics.get('math_depth', 0.0):.2f} / 3.0
- 筆者オピニオン度スコア: {feedback_metrics.get('stance', 0.0):.2f} / 3.0
- 知的好奇心刺激度スコア: {feedback_metrics.get('appeal', 0.0):.2f} / 3.0
- 総合品質スコア: {feedback_metrics.get('total_score', 0.0):.2f} / 9.0
- 指摘されたボトルネック: {diagnosis}
- 「あなたの意見は？」肩透かしリスク: {feedback_metrics.get('lack_of_opinion_risk', 0.0)*100:.1f}%

【今回のリライトにおける必須改善指令】
{focus_text}

---
【対象論文情報】
- arXiv ID: {paper['arxiv_id']}
- タイトル: {paper['title']}
- 著者: {', '.join(paper['authors'])}
- アブストラクト: {paper['summary']}
{full_text_section}
---
【前回のドラフト（第{revision_round - 1}稿）】
{previous_draft}

---
【リライト時のフォーマット規則】
1. 前回のドラフトの構成を維持したまま、改善指令を反映して全面的にブラッシュアップしてください。
2. 太字強調は必ず HTML の <strong> タグを使用し、Markdownの ** は一切使用しないでください。
3. 本文先頭に「# タイトル」は置かず、フロントマターから始めてください。
4. 数式ブロックは必ず独立した行（$$\n数式\n$$）で出力してください。
5. 前回のドラフトに含まれる図表プレースホルダーや画像構文は削除せず、適切な位置に配置してください。

Markdown形式で出力してください。
"""

    _, fig_parts = build_figures_prompt_components(paper.get("figures"))
    rewrite_contents = fig_parts + [rewrite_prompt]

    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash",
    ]
    rewritten_text = None
    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=rewrite_contents,
            )
            rewritten_text = response.text
            print(f"  ✓ Gemini リライト完了 (Round {revision_round}, モデル: {model_name})")
            break
        except Exception as e:
            print(f"  ⚠️ {model_name} でのリライトエラー: {e}")

    if not rewritten_text:
        print("  ⚠️ リライト生成に失敗したため、前回のドラフトを維持します。")
        return previous_draft

    return rewritten_text


def generate_refined_blog_post(
    paper: Dict[str, Any],
    rank: int = 1,
    max_revisions: int = 2,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    genre: Optional[str] = "auto",
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """Evaluator-Optimizer パターンによる自律改善パイプライン"""
    resolved_genre = resolve_paper_genre(paper, genre)
    current_post = write_blog_post_with_gemini(paper, rank=rank, relevant_posts=relevant_posts, genre=resolved_genre)
    metrics = verify_post_with_jev(current_post, round_num=1, domain=resolved_genre)
    history = [metrics]

    round_count = 1
    while not metrics.get("passed", False) and round_count <= max_revisions:
        round_count += 1
        print(f"\n⚡ 基準未達のため、Jevの改善指示を反映してリライトを実行します (Revision {round_count - 1}/{max_revisions})...")

        current_post = rewrite_blog_post_with_gemini(
            paper=paper,
            previous_draft=current_post,
            feedback_metrics=metrics,
            revision_round=round_count,
            rank=rank,
            relevant_posts=relevant_posts,
            genre=resolved_genre,
        )

        metrics = verify_post_with_jev(current_post, round_num=round_count, domain=resolved_genre)
        history.append(metrics)

        if metrics.get("passed", False):
            print(f"\n🎉 Jev の厳格品質基準をクリアしました！ (Round {round_count})")
            break

    if not metrics.get("passed", False):
        print(f"\n⚠️ 最大リビジョン数 ({max_revisions}回) に達しました。現時点で最高品質の原稿を採用します。")

    return current_post, metrics, history
