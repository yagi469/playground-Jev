"""
generators/doc_generator.py
===========================
ローカルドキュメント（PDF / Markdown / 書籍）向け Gemini 執筆、Jev 品質検証、
Evaluator-Optimizer 自律リライト推敲ループ
"""

from typing import List, Dict, Any, Optional, Tuple
from config import init_gemini_client
from core.figure_extractor import build_figures_prompt_components
from generators.prompts import get_genre_blog_config
from score_post import verify_post_with_jev


def write_blog_post_from_doc_with_gemini(
    doc_info: Dict[str, Any],
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """PDF または Markdown の内容から、Gemini でジャンル適応型の本格解説ブログ記事（初稿）を執筆"""
    client = init_gemini_client()

    genre = doc_info.get("genre", "general")
    print(f"\n✍️ [Gemini 執筆] '{doc_info['title']}' (ジャンル: {genre}) の解説ブログ記事を自律生成中...")

    g_config = get_genre_blog_config(genre, doc_info)

    chapter_focus = f"- フォーカスする章・テーマ: {doc_info['chapter_hint']}\n" if doc_info.get("chapter_hint") else ""
    page_focus = f"- 抽出範囲: {doc_info['page_label']}\n" if doc_info.get("page_label") else ""

    related_context = ""
    if relevant_posts:
        lines = []
        for rp in relevant_posts:
            lines.append(f"- [{rp['title']}]({rp['url']}) (概要: {rp.get('summary', '')[:100]})")
        related_context = f"\n【当ブログの関連する過去記事（文脈の記憶）】\n本文の論理展開の中で、関連する概念や先行理論に触れる際、自然に以下の過去記事への言及・内部リンク（例: [タイトル](/posts/slug)）を1〜2箇所織り交ぜて、ブログ全体の知識ネットワークを有機的に繋げてください：\n" + "\n".join(lines) + "\n"

    tags_sample = "\n".join([f"  - {t}" for t in g_config["default_tags"]])

    figures_instruction, fig_parts = build_figures_prompt_components(doc_info.get("figures"))

    prompt = f"""{g_config['persona']}
読者が「で、あなたの意見は？」と突っ込みたくなるような退屈なAIまとめ記事ではなく、
著者の思考の核心と現実への影響・独自オピニオンを生き生きと語り尽くす、知的好奇心を刺激する熱いブログ記事を執筆してください。
{related_context}{figures_instruction}
【取り上げるドキュメント情報】
- 文書名: {doc_info['file_name']}
- タイトル: {doc_info['title']}
- 著者/編者: {', '.join(doc_info.get('authors', ['-']))}
- 分野/カテゴリ: {', '.join(doc_info.get('categories', g_config['default_tags']))}
- 専門領域: {doc_info.get('subfield', '一般')}
- 判定ジャンル: {genre}
{page_focus}{chapter_focus}- 概要:
{doc_info.get('summary', '')}

---
【記事の構成とフォーマット規則】
1. **フロントマター（YAML Frontmatter）を記事先頭に必ず出力してください**:
---
title: "思わずクリックしたくなる、知的好奇心と本質を突いた日本語タイトル"
summary: "120〜180文字程度の魅力的な記事要約（何が論じられ、どのような実践的・知的好奇心があるのかが伝わる文章）"
tags:
{tags_sample}
  - （ドキュメント内容に即したタグを3〜5個。スラッシュは使わずハイフンを使用）
---

2. **太字・強調ルールの遵守（最重要）**:
   - ブログ記事内でテキストを太字・強調する場合は、必ず HTMLの <strong> タグ（例: <strong>太字テキスト</strong>）を使用してください。

3. **本文の見出し構成**:
   - 本文の開始部分に「# タイトル」を置かないでください。
   - 本文の見出しは「## （見出し名）」から始めてください。
   - 以下の構成で執筆してください：
{g_config['sections']}

4. **執筆ガイドライン**:
   {g_config['guidance']}

Markdown形式で出力してください。
"""

    if doc_info["doc_type"] == "pdf":
        contents = [doc_info["pdf_part"]] + fig_parts + [prompt]
    else:
        text_body = doc_info.get("text_content", "")[:35000]
        contents = [f"【ドキュメント本文】\n{text_body}\n\n"] + fig_parts + [prompt]

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
                contents=contents,
            )
            post_text = response.text
            print(f"  ✓ Gemini 執筆完了 (モデル: {model_name})")
            break
        except Exception as e:
            print(f"  ⚠️ {model_name} でのエラー: {e}")

    if not post_text:
        raise RuntimeError("Gemini による執筆に失敗しました。")

    return post_text


def rewrite_doc_blog_post_with_gemini(
    doc_info: Dict[str, Any],
    previous_draft: str,
    feedback_metrics: Dict[str, Any],
    revision_round: int,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Jev のフィードバックに基づきドキュメント解説記事を再推敲"""
    client = init_gemini_client()

    genre = doc_info.get("genre", "general")
    g_config = get_genre_blog_config(genre, doc_info)

    print(f"\n🔄 [Gemini リライト Round {revision_round}] Jevの改善フィードバック（ジャンル: {genre}）を反映してドキュメント記事を再推敲中...")

    diagnosis = feedback_metrics.get("diagnosis", "")
    focus_instructions = []

    if genre in ["business", "management"]:
        if feedback_metrics.get("clarity", 0.0) < 2.0 or diagnosis == "need_pedagogical_steps":
            focus_instructions.append(
                "- 【最重要：前提の課題と因果関係の明快な解説】前提となる組織課題や具体例を補強し、"
                "なぜそのフレームワークやプロセスになるのかという因果関係を読者が納得できるように丁寧に解き明かしてください。"
            )
        if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
            focus_instructions.append(
                "- 【最重要：モデル・組織ダイナミクスの具体化】抽象的なお茶濁しや精神論を排除し、"
                "著者が提示する具体的なフレームワーク、生産・組織モデル、評価制度のロジックを掘り下げて解説してください。"
            )
        if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
            focus_instructions.append(
                "- 【最重要：オピニオンの徹底強化】「で、私（筆者）はどう考えるか？」セクションを強化してください。"
                "当たり障りのない本の要約を脱し、現代のIT/スタートアップ組織や個人の働き方に照らし合わせた独自スタンスを熱量高く語ってください。"
            )
        if diagnosis == "avoid_shallow_metaphors":
            focus_instructions.append(
                "- 【安易な常識・クリシェの排除】ビジネス書のありふれたスローガンや陳腐な精神論を排除し、骨太な洞察に徹してください。"
            )
    elif genre in ["tech", "engineering"]:
        subfield = str(doc_info.get("subfield", "")).lower()
        title_lower = str(doc_info.get("title", "")).lower()
        is_writing_or_culture = any(
            k in subfield or k in title_lower
            for k in ["writing", "blog", "文章", "ライティング", "キャリア", "career", "文化", "culture", "コミュニケーション", "communication", "習慣", "チーム"]
        )
        if feedback_metrics.get("clarity", 0.0) < 2.0 or diagnosis == "need_pedagogical_steps":
            focus_instructions.append(
                "- 【最重要：前提知識と動作ステップの丁寧な解説】専門用語をいきなり並べず、前提知識を定義し、"
                "システムや実践プロセスがどのように問題を解決するのかのステップを明快に解説してください。"
            )
        if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
            if is_writing_or_culture:
                focus_instructions.append(
                    "- 【最重要：実践的思考法・アプローチの具体化】抽象論やお茶濁しを排除し、"
                    "本書が提示する言語化プロセスや具体的な実践ステップを掘り下げて解説してください。"
                )
            else:
                focus_instructions.append(
                    "- 【最重要：アーキテクチャ・内部メカニズムの具体化】具体的なデータ構造、通信、設計パターンを具体的に掘り下げてください。"
                )
        if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
            focus_instructions.append(
                "- 【最重要：エンジニアリングオピニオンの強化】現場目線の実践的オピニオンやスタンスを鮮明に打ち出してください。"
            )
    elif genre in ["classics", "history", "humanities", "philosophy"]:
        if feedback_metrics.get("clarity", 0.0) < 2.0 or diagnosis == "need_pedagogical_steps":
            focus_instructions.append(
                "- 【最重要：時代背景と人間ドラマの情景描写】時代背景や過酷な状況、人物相関をより丁寧に補足し、"
                "現代の読者にも情景が目に浮かぶように明快に解説してください。"
            )
        if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
            focus_instructions.append(
                "- 【最重要：核心エピソードと知略の具体化】表面的なあらすじ要約にとどまらず、"
                "登場人物の決断や心理、知略（メーティス）のメカニズムを具体的に掘り下げてください。"
            )
        if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
            focus_instructions.append(
                "- 【最重要：現代へのアナロジーとオピニオンの徹底強化】「で、私（筆者）はどう考えるか？」セクションで、"
                "現代のビジネス、キャリア、意思決定、人間心理に引きつけた骨太なオピニオンを熱量高く語ってください。"
            )
    else:
        if feedback_metrics.get("clarity", 0.0) < 2.0 or feedback_metrics.get("rushed_math_risk", 0.0) >= 0.35 or diagnosis == "need_pedagogical_steps":
            focus_instructions.append(
                "- 【最重要：前提知識と途中計算（行間）の徹底解説】読者が置いてけぼりにならないよう、"
                "記号や概念の定義、論理展開のステップを明快に解説してください。"
            )
        if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
            focus_instructions.append(
                "- 【最重要：機構・思考モデルの具体化】具体的な思考モデルやロジックがどう機能しているのかを明快に解説してください。"
            )
        if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
            focus_instructions.append(
                "- 【最重要：オピニオンの徹底強化】「で、私（筆者）はどう考えるか？」セクションを強化してください。"
            )

    if not focus_instructions:
        focus_instructions.append(
            "- 前回のドラフト全体の論理のキレと筆者の独自スタンスをさらに研ぎ澄ましてください。"
        )

    focus_text = "\n".join(focus_instructions)

    related_reminder = ""
    if relevant_posts:
        lines = [f"- [{rp['title']}]({rp['url']})" for rp in relevant_posts]
        related_reminder = "\n【過去記事へのリンク維持・活用】\n" + "\n".join(lines) + "\n関連する過去記事への内部リンクが自然に含まれていることを確認してください。\n"

    rewrite_prompt = f"""{g_config['persona']}

先ほどあなたが執筆したブログ記事ドラフトに対し、Jev System One 診断システムから以下の品質診断スコアと改善要求が届きました。

【Jev による前稿（第{revision_round - 1}稿）の診断結果】
- 具体性・深さスコア: {feedback_metrics.get('math_depth', 0.0):.2f} / 3.0
- 筆者オピニオン度スコア: {feedback_metrics.get('stance', 0.0):.2f} / 3.0
- 知的好奇心刺激度スコア: {feedback_metrics.get('appeal', 0.0):.2f} / 3.0
- 総合品質スコア: {feedback_metrics.get('total_score', 0.0):.2f} / 9.0
- 指摘されたボトルネック: {diagnosis}
- 「あなたの意見は？」肩透かしリスク: {feedback_metrics.get('lack_of_opinion_risk', 0.0)*100:.1f}%

【今回のリライトにおける必須改善指令】
{focus_text}
{related_reminder}
---
【対象ドキュメント情報】
- 文書名: {doc_info['file_name']}
- タイトル: {doc_info['title']}
- 対象範囲: {doc_info.get('page_label', '')} / {doc_info.get('chapter_hint', '')}
- 判定ジャンル: {genre}

---
【前回のドラフト】
{previous_draft}

---
【フォーマット再確認】
1. フロントマター（YAML）を必ず先頭に出力
2. テキストの太字は必ず HTMLの <strong>太字</strong> タグを使用（Markdownの ** は禁止）
3. 本文開始に「# タイトル」を置かない（## 見出しから開始）
4. {g_config['guidance']}
5. 前回のドラフトに含まれる図表プレースホルダーや画像構文は削除せず、適切な位置に配置してください。

以上の指示に従い、完全版 Markdown 記事を出力してください。
"""

    _, fig_parts = build_figures_prompt_components(doc_info.get("figures"))

    if doc_info["doc_type"] == "pdf":
        contents = [doc_info["pdf_part"]] + fig_parts + [rewrite_prompt]
    else:
        text_body = doc_info.get("text_content", "")[:35000]
        contents = [f"【ドキュメント本文】\n{text_body}\n\n"] + fig_parts + [rewrite_prompt]

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
                contents=contents,
            )
            post_text = response.text
            print(f"  ✓ Gemini リライト完了 (モデル: {model_name})")
            break
        except Exception as e:
            print(f"  ⚠️ {model_name} でのエラー: {e}")

    if not post_text:
        raise RuntimeError("Gemini によるリライトに失敗しました。")

    return post_text


def generate_refined_doc_blog_post(
    doc_info: Dict[str, Any],
    max_revisions: int = 2,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """Gemini 執筆 ➡️ Jev 診断 ➡️ 必要に応じ Gemini リライトの自律推敲ループ"""
    genre = doc_info.get("genre", "general")
    domain_for_jev = (
        "business" if genre in ["business", "management"]
        else ("tech" if genre in ["tech", "engineering"]
        else ("physics" if genre in ["physics", "math"]
        else ("classics" if genre in ["classics", "history", "humanities", "philosophy"]
        else "general")))
    )

    current_draft = write_blog_post_from_doc_with_gemini(doc_info, relevant_posts=relevant_posts)

    history = []
    quality = verify_post_with_jev(current_draft, round_num=1, domain=domain_for_jev)
    history.append(quality)

    revision_round = 1
    while not quality["passed"] and revision_round <= max_revisions:
        revision_round += 1
        print(f"\n⚡ [推敲ループ] 第{revision_round - 1}稿は品質基準未達のため、Jevの指摘を反映して再推敲を実行します (Round {revision_round})")

        try:
            current_draft = rewrite_doc_blog_post_with_gemini(
                doc_info=doc_info,
                previous_draft=current_draft,
                feedback_metrics=quality,
                revision_round=revision_round,
                relevant_posts=relevant_posts,
            )
            quality = verify_post_with_jev(current_draft, round_num=revision_round, domain=domain_for_jev)
            history.append(quality)
        except Exception as e:
            print(f"⚠️ リライト中にエラーが発生したため、前回のドラフトを採用します: {e}")
            break

    if quality["passed"]:
        print(f" ✨ Jev 品質基準を見事クリアしました！（総合スコア: {quality['total_score']:.2f}点）")
    else:
        print(f" ⚠️ リビジョン上限（{max_revisions}回）に達したため、現時点での最高推敲版を採用します。")

    return current_draft, quality, history
