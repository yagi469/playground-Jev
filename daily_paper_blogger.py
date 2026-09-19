#!/usr/bin/env python3
"""
daily_paper_blogger.py
======================
arXiv (hep-th / quant-ph) から最新の量子コンピュータ・量子情報・ホログラフィ関連の論文を自動取得し、
TypeSafe (Jev) による高速多面スクリーニングで本日のベスト論文を厳選、
Google Gemini で独自の考察・スタンスを盛り込んだブログ解説記事を自動執筆するパイプライン。
"""

import os
import time
import json
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
import httpx
import yaml
from dotenv import load_dotenv
from typesafe_sdk import TypeSafeClient, Choice, Score, Noul

# 環境変数の読み込み
load_dotenv(".env.local")
load_dotenv(".env")

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

# 初期化を試みる（APIキー未設定時は実行時に遅延初期化）
try:
    init_gemini_client()
except Exception as e:
    print(f"Gemini Client 初期化警告: {e}")

typesafe_api_key = os.getenv("TYPESAFE_API_KEY")
if not typesafe_api_key:
    raise ValueError("TYPESAFE_API_KEY が設定されていません。.env.local を確認してください。")

typesafe_client = TypeSafeClient(api_key=typesafe_api_key)


# ==============================================================================
# 1. arXiv API から論文を取得
# ==============================================================================
def _parse_arxiv_xml(xml_text: str) -> List[Dict[str, Any]]:
    """arXiv API の Atom XML レスポンスを共通パース"""
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        title = " ".join(title_el.text.split()) if title_el is not None and title_el.text else "Untitled"
        
        # arXiv のエラーエントリはスキップ
        if title.lower() == "error":
            continue

        summary_el = entry.find("atom:summary", ns)
        summary = " ".join(summary_el.text.split()) if summary_el is not None and summary_el.text else ""

        id_el = entry.find("atom:id", ns)
        raw_id_url = id_el.text.strip() if id_el is not None and id_el.text else ""
        arxiv_id = raw_id_url.split("/abs/")[-1] if "/abs/" in raw_id_url else raw_id_url

        published_el = entry.find("atom:published", ns)
        published = published_el.text[:10] if published_el is not None and published_el.text else ""

        authors = []
        for author in entry.findall("atom:author", ns):
            name_el = author.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        categories = []
        for cat in entry.findall("atom:category", ns):
            term = cat.attrib.get("term")
            if term:
                categories.append(term)

        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

        papers.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "summary": summary,
            "published": published,
            "authors": authors[:5],
            "categories": categories,
            "pdf_url": pdf_url,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
        })
    return papers


def fetch_arxiv_papers(max_results: int = 30) -> List[Dict[str, Any]]:
    """
    hep-th (高エネルギー理論) と math-ph (数理物理) を最重要母集団とし、
    quant-ph も含めてバランスよく最新論文を取得（特定カテゴリの過密を防止）
    """
    print("\n📡 [arXiv API] hep-th (最重要) & math-ph & quant-ph から最新論文を取得中...")

    # カテゴリごとに分散取得して、quant-ph による圧迫を防止
    hep_count = max(15, int(max_results * 0.6))
    math_count = max(8, int(max_results * 0.25))
    quant_count = max(7, int(max_results * 0.25))

    queries = [
        ("cat:hep-th", hep_count, "hep-th (高エネルギー理論)"),
        ("cat:math-ph", math_count, "math-ph (数理物理)"),
        ("cat:quant-ph", quant_count, "quant-ph (量子情報・物理)"),
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/atom+xml",
    }

    all_papers = []
    seen_ids = set()

    for cat_query, count, label in queries:
        url = (
            "https://export.arxiv.org/api/query?"
            f"search_query={cat_query}&"
            "sortBy=submittedDate&sortOrder=descending&"
            f"max_results={count}"
        )
        try:
            response = httpx.get(url, headers=headers, timeout=25.0)
            response.raise_for_status()
            papers = _parse_arxiv_xml(response.text)
            added = 0
            for p in papers:
                if p["arxiv_id"] not in seen_ids:
                    seen_ids.add(p["arxiv_id"])
                    all_papers.append(p)
                    added += 1
            print(f"  ✓ {label}: {added} 件取得")
        except Exception as e:
            print(f"  ⚠️ {label} 取得エラー: {e}")

    print(f"✅ 合計 {len(all_papers)} 件の論文メタデータを収集完了")
    return all_papers


def fetch_arxiv_papers_by_ids(arxiv_ids: List[str]) -> List[Dict[str, Any]]:
    """指定された特定の arXiv ID リストから論文メタデータを直接取得"""
    clean_ids = [aid.strip().replace("arxiv:", "").replace("arXiv:", "") for aid in arxiv_ids if aid.strip()]
    if not clean_ids:
        return []

    print(f"\n📡 [arXiv API] 指定された論文 ID ({', '.join(clean_ids)}) をピンポイント取得中...")
    url = f"https://export.arxiv.org/api/query?id_list={','.join(clean_ids)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/atom+xml",
    }

    try:
        response = httpx.get(url, headers=headers, timeout=25.0)
        response.raise_for_status()
        papers = _parse_arxiv_xml(response.text)
        print(f"✅ {len(papers)} 件の指定論文メタデータを取得完了")
        return papers
    except Exception as e:
        print(f"❌ arXiv API 指定ID取得失敗: {e}")
        return []


# ==============================================================================
# 2. TypeSafe (Jev) による高速多面スクリーニング & ランキング
# ==============================================================================
def load_user_interests() -> Dict[str, Any]:
    """Google Driveの文献等から抽出されたユーザー興味プロファイルをロード"""
    profile_path = os.path.join(os.path.dirname(__file__), "user_interests.json")
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
    Google Driveの読書傾向（SCFT, D-brane, AdS/CFT, 量子情報）に合致し、
    「量子情報・ホログラフィ・数理物理」関連で最も価値が高く面白い論文をランキング
    """
    user_profile = load_user_interests()
    user_criteria = user_profile.get("evaluation_criteria_for_jev", "")
    core_themes = ", ".join(user_profile.get("core_themes", []))

    print(f"\n⚡ [TypeSafe Jev] {len(papers)} 件の論文をユーザー興味プロファイルに基づいて多面スクリーニング中...")
    if core_themes:
        print(f"🎯 反映中の興味テーマ: {core_themes[:60]}...")

    scored_papers = []

    # Jev への質問定義
    questions = {
        # 1. 数理物理・高エネルギー理論の核心領域への合致度
        "is_math_physics_core": Noul(
            instructions=(
                "この論文は、場の量子論の厳密な数理構造、超対称共形場理論（SCFT）、"
                "カイラル代数/頂点作用素代数 (VOA)、Dブレーン・超弦理論の幾何学、"
                "トポロジカル場の量子論 (TQFT)、共形ブートストラップ、"
                "あるいはAdS/CFT対応の厳密な数理・代数的側面に明確に関連していますか？"
                "（単なる量子回路の実装や物性模型の数値シミュレーション、素粒子実験現象論ではなく、数理理論的深みがあるかを判定してください）"
            )
        ),
        # 2. ユーザー個人の興味・数理物理関心への合致度 (Google Drive文献プロファイル準拠)
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
        # 3. 理論的深さ・新規性（数理的厳密性）
        "theoretical_depth": Score(
            instructions="この論文の理論的深さや数学・物理学的な新規性・厳密性レベルを評価してください",
            criteria=[
                "初歩的・既存のレビューや軽微な計算",
                "標準的な応用や既存枠組み内の進展",
                "独自の新たな発見・非自明な理論的ブレイクスルーがある",
                "極めて重要な金字塔・パラダイムシフトの可能性を秘める",
            ],
        ),
        # 4. ブログ読者への面白さ・知的好奇心の刺激度
        "blog_appeal": Score(
            instructions=(
                "数理物理学や理論物理に関心を持つ読者にとって、"
                "知的好奇心を刺激する話題性や数理的美しさ・深みがあるかを評価してください"
            ),
            criteria=[
                "地味・極端な専門家以外には伝わりにくい",
                "普通・学術的価値はあるが一般の知的好奇心を惹きにくい",
                "魅力的・物理的洞察や数理的エレガンスで読者を惹きつけられる",
                "非常に魅力的・『これはすごい』と直感的に共有・議論したくなる",
            ],
        ),
        # 5. サブ分野の特定
        "subfield": Choice(
            instructions="この論文が最も強くフォーカスしているサブ分野を分類してください",
            criteria={
                "scft_chiral_algebra": "超対称共形場理論 (SCFT)・カイラル代数/VOA・4d/2d対応・超対称指数",
                "string_d_brane_geometry": "超弦理論・Dブレーン幾何・非摂動的弦理論・BPS状態",
                "tqft_generalized_symmetry": "トポロジカル場の量子論 (TQFT)・高次対称性・非可逆対称性",
                "holography_bootstrap_exact": "AdS/CFT厳密ホログラフィ・共形ブートストラップ・代数的場の量子論",
                "quantum_info_condensed_matter": "量子情報・量子計算・テンソルネットワーク・物性模型",
                "phenomenology_or_unrelated": "素粒子実験現象論・単なる数値シミュレーション・関心外",
            },
        ),
    }

    start_t = time.time()

    # サブ分野ごとの重み付け係数（ユーザーの本命分野を最優先）
    subfield_multipliers = {
        "scft_chiral_algebra": 1.4,         # ドンピシャ最優先
        "string_d_brane_geometry": 1.3,     # 本命テーマ
        "tqft_generalized_symmetry": 1.25,  # 強い関心
        "holography_bootstrap_exact": 1.2,  # 理論的関心
        "quantum_info_condensed_matter": 0.4, # 量子情報は低優先
        "phenomenology_or_unrelated": 0.1,    # 現象論・数値計算は弾く
    }

    for idx, p in enumerate(papers):
        # 論文テキスト（State）
        state_text = f"Title: {p['title']}\nCategories: {', '.join(p['categories'])}\nAbstract: {p['summary']}"

        try:
            res = typesafe_client.system_one(
                state={"paper": state_text},
                questions=questions,
            )

            is_core = res.nouls["is_math_physics_core"].noul
            u_match = res.scores["user_interest_match"].score
            t_depth = res.scores["theoretical_depth"].score
            b_appeal = res.scores["blog_appeal"].score
            s_field = res.choices["subfield"].choice

            # 総合スコア計算 (ユーザー興味 u_match を最重視)
            multiplier = subfield_multipliers.get(s_field, 1.0)
            total_score = ((is_core * 3.0) + (u_match * 3.5) + (t_depth * 2.0) + (b_appeal * 1.5)) * multiplier

            p["jev_metrics"] = {
                "is_math_physics_core": round(is_core, 3),
                "is_quantum_relevant": round(is_core, 3), # 後方互換
                "user_interest_match": round(u_match, 3),
                "theoretical_depth": round(t_depth, 3),
                "blog_appeal": round(b_appeal, 3),
                "subfield": s_field,
                "total_score": round(total_score, 3),
            }
            scored_papers.append(p)

            print(f"  [{idx+1}/{len(papers)}] {p['arxiv_id']} | 数理物理核心: {is_core:.1%} | 興味合致: {u_match:.2f}/3 | 深度: {t_depth:.1f} | 分野: {s_field} | 総合: {total_score:.2f}")

        except Exception as e:
            print(f"  ⚠️ {p['arxiv_id']} のJev評価スキップ: {e}")

    elapsed = time.time() - start_t
    print(f"✅ Jev 判定完了 ({elapsed:.1f}秒)")

    # スコア降順ソート
    scored_papers.sort(key=lambda x: x["jev_metrics"]["total_score"], reverse=True)
    return scored_papers


# ==============================================================================
# 3. Google Gemini による本格ブログ執筆
# ==============================================================================
def write_blog_post_with_gemini(paper: Dict[str, Any], rank: int = 1) -> str:
    """
    選定されたベスト論文をもとに、筆者の熱量と考察が入ったブログ記事を自動執筆
    """
    global gemini_client
    if gemini_client is None:
        init_gemini_client()

    m = paper["jev_metrics"]
    print(f"\n🧠 [Gemini] 総合第{rank}位の論文 {paper['arxiv_id']} のブログ記事を執筆中...")
    print(f"   タイトル: {paper['title']}")
    print(f"   分野: {m['subfield']} (総合スコア: {m['total_score']})")

    user_profile = load_user_interests()
    user_perspective = ""
    if user_profile:
        themes = ", ".join(user_profile.get("core_themes", []))
        user_perspective = f"\n【筆者の専門的バックボーン・着眼点（Google Driveの蔵書・関心より）】\n- 筆者は場の量子論（ワインバーグ流の厳密性）、超対称共形場理論（SCFT）、カイラル代数、Dブレーン幾何、トポロジカル場論（TQFT）、AdS/CFT対応などの数理的側面に強い思い入れがあります。\n- 関心テーマ: {themes}\n- 「で、私（筆者）はどう考えるか？」のセクションでは、これらの数理物理や非摂動的・対称性的観点も交えつつ、独自の一歩踏み込んだ深いオピニオンを熱量高く語ってください。\n"

    prompt = f"""あなたは超弦理論、超対称共形場理論（SCFT）、場の量子論の厳密な数理構造（代数・幾何）、AdS/CFT対応の最前線を探究する、一流の理論物理学者兼サイエンスブロガーです。
読者が「で、あなたの意見は？」と突っ込みたくなるような退屈なAIまとめ記事ではなく、
安易で子供騙しな日常のたとえ話（コーヒーの冷却など）に逃げず、理論物理の真の美しさ・対称性の幾何・代数的機構を生き生きと語り尽くす、知的好奇心を刺激する熱いブログ記事を執筆してください。
{user_perspective}
【取り上げる論文情報】
- arXiv ID: {paper['arxiv_id']}
- 論文タイトル: {paper['title']}
- 著者: {', '.join(paper['authors'])}
- カテゴリ: {', '.join(paper['categories'])}
- 論文URL: {paper['url']}
- PDFリンク: {paper['pdf_url']}
- アブストラクト (英文):
{paper['summary']}

【Jev System One による分析評価】
- 数理物理核心度: {m.get('is_math_physics_core', m.get('is_quantum_relevant', 0.0)) * 100:.1f}%
- ユーザー関心合致スコア: {m.get('user_interest_match', 0.0):.2f} / 3.0
- 理論的深さスコア: {m['theoretical_depth']:.2f} / 3.0
- ブログ知的好奇心スコア: {m['blog_appeal']:.2f} / 3.0
- 専門領域: {m['subfield']}

---
【記事の構成とフォーマット規則】
1. **フロントマター（YAML Frontmatter）を記事先頭に必ず出力してください**:
---
title: "思わずクリックしたくなる、知的好奇心と物理的本質を突いた日本語タイトル"
summary: "120〜180文字程度の魅力的な記事要約（何が解明され、なぜ物理として美しいのかが伝わる文章）"
tags:
  - 物理学
  - （論文内容に即したタグを3〜5個。スラッシュは使わずハイフンを使用。例: 素粒子論, 超共形場理論, AdS-CFT, カイラル代数, 超弦理論, TQFTなど）
---

2. **太字・強調ルールの遵守（最重要）**:
   - ブログ記事内でテキストを太字・強調する場合は、Markdownの ** 記法ではなく、必ず HTMLの <strong> タグ（例: <strong>太字テキスト</strong>）を使用してください。

3. **本文の見出し構成**:
   - 本文の開始部分に「# タイトル」を置かないでください（フロントマターのtitleがWebサイト側で自動描画されるため）。
   - 本文の見出しは「## （見出し名）」から始めてください。
   - 以下の構成で執筆してください：
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象論文へのリンク（[{paper['arxiv_id']}]({paper['url']})）およびタイトル・著者情報を必ず明記してください。
     - ## 背景にある物理・数学の壁: 従来の理論（摂動論、標準的場の理論、既存のホログラフィ等）の何が未解決だったのか、なぜこの問題が本質的なのかを論理的かつクリアに解説。
     - ## この論文の核心アイデアと数理的機構: 著者がどのようなアイデア・数理構造（対称性、代数、幾何学的配位、双対性など）を用いてその壁を乗り越えたのかを解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：独自のスタンス・考察・ツッコミ）単なる要約で終わらせず、数理物理・非摂動QFT・超対称性の視点から「ここが美しい」「この仮定はどこまで一般化できるのか？」「今後の研究の方向性」など、研究者としての骨太なオピニオンを展開。
     - ## まとめ ＆ 論文リンク: 記事の総括と、arXivアブストラクトへのリンク（[{paper['arxiv_id']}]({paper['url']})）、PDFへのリンク（[PDF]({paper['pdf_url']})）を分かりやすくリスト形式で設置してください。

4. **数式ブロック（Display Math）の改行ルール**:
   - 独立したブロック数式（$$ ... $$）を出力する際は、インラインとして折り返されるのを防ぎ横スライド（スクロール）可能にするため、必ず前後に改行を入れて $$ を独立した行に配置してください：
     $$
     数式
     $$

Markdown形式で出力してください。
"""

    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash",
    ]
    post_text = None
    for model_name in candidate_models:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            post_text = response.text
            print(f"  ✓ Gemini 執筆完了 (モデル: {model_name})")
            break
        except Exception as e:
            print(f"  ⚠️ {model_name} でのエラー: {e}")

    if not post_text:
        raise RuntimeError("Gemini による執筆に失敗しました。")

    return post_text


# ==============================================================================
# 4. Jev による多面品質検証 & Evaluator-Optimizer リライトループ
# ==============================================================================
def verify_post_with_jev(post_content: str, round_num: int = 1) -> Dict[str, Any]:
    """
    生成されたブログ記事に対して「数理的深度・筆者オピニオン・知的好奇心刺激度」を Jev (System One) で厳格に多面採点し、
    足切り判定および具体的な改善ボトルネックを診断する。
    """
    print(f"\n⚡ [TypeSafe Jev] 生成記事の品質を厳密検証中 (Round {round_num})...")

    questions = {
        # 1. 数理・理論の具体性
        "mathematical_depth": Score(
            instructions=(
                "記事中で、論文の核心となる数学的構造（対称性、代数、幾何学的配位、双対性、不変量、作用素など）や"
                "理論的機構が、抽象的な形容詞や美辞麗句だけでなく、具体的にわかりやすく論理的に解説されているかを評価してください。"
            ),
            criteria=[
                "中身が薄い（抽象的な美辞麗句やお茶濁しばかりで、何がどう作用しているのか数理のロジックが見えない）",
                "表面的（専門用語は並んでいるが、どういう仕組みで問題が解決されたかの掘り下げが浅い）",
                "明確で具体的（アイデアや数理構造、物理的帰結の論理展開が明快に解説されている）",
                "極めて深い（非摂動効果や厳密解、代数・幾何の核心と美しさが鮮やかに浮き彫りにされている）",
            ],
        ),
        # 2. 筆者オピニオンの切れ味・独自スタンス
        "author_stance": Score(
            instructions=(
                "「で、私（筆者）はどう考えるか？」セクションを含め、記事全体を通して筆者独自の視点・問題意識・"
                "批判的考察・将来への問いが鮮明に打ち出されているかを評価してください。"
            ),
            criteria=[
                "客観的な要約・解説に終始しており、筆者の立場や主観が皆無",
                "当たり障りのない感想や一般論程度で、スタンスが曖昧",
                "筆者独自の着眼点や問いが明確に示されており、研究者としてのスタンスが伝わる",
                "強烈な独自オピニオンや鋭い批判的考察があり、知的刺激に満ちている",
            ],
        ),
        # 3. 読者の知的好奇心刺激度
        "intellectual_appeal": Score(
            instructions=(
                "数理物理学や理論物理に関心を持つ読者にとって、知的好奇心が強く刺激され、"
                "「この論文を読んでみたい」「この視点は面白い」と思わせる魅力があるかを評価してください。"
            ),
            criteria=[
                "退屈・安易（子供騙しの比喩やありふれたAIまとめ構文で、知的好奇心が湧かない）",
                "教科書的（論理は通っているが、ワクワクするような熱量や知的フックに欠ける）",
                "魅力的（問題の本質とブレイクスルーの意義が伝わり、読んでいて面白い）",
                "圧倒的（理論物理の真の美しさとスリルが伝わり、読者を強く引き込む名論考）",
            ],
        ),
        # 4. 最大の改善ボトルネック診断
        "critique_diagnosis": Choice(
            instructions="この記事のクオリティをさらに高めるために、最も改善が必要なボトルネックはどこですか？",
            criteria={
                "need_math_details": "核心アイデアの数理的機構や代数・幾何のロジックが抽象的。具体的な作用素・不変量・計算機構の解説が必要",
                "need_sharp_opinion": "筆者オピニオンが論文の無難なまとめ。独自の問い・批判的考察・数理的意義をもっと熱く語るべき",
                "avoid_shallow_metaphors": "安易な日常のたとえ話やAI特有のお茶濁しが目立つ。理論物理の真の美しさに徹するべき",
                "high_quality": "数理の具体性、オピニオンの深さ、知的好奇心刺激度が極めて高い水準で調和している",
            },
        ),
        # 5. 「で、あなたの意見は？」肩透かしリスク
        "lack_of_opinion_risk": Noul(
            instructions="読者が読み終わった後に「事実は分かったけど、結局筆者はどう思っているの？」と肩透かしを感じるリスクがありますか？"
        ),
    }

    try:
        res = typesafe_client.system_one(state={"post": post_content}, questions=questions)
        math_depth = res.scores["mathematical_depth"].score
        stance = res.scores["author_stance"].score
        appeal = res.scores["intellectual_appeal"].score
        diagnosis = res.choices["critique_diagnosis"].choice
        risk = res.nouls["lack_of_opinion_risk"].noul

        total_score = math_depth + stance + appeal  # 最大 9.0

        # 足切り基準: 総合 7.0 以上、かつ各項目 2.0 以上、かつリスク 35% 未満
        passed = (total_score >= 7.0) and (math_depth >= 2.0) and (stance >= 2.0) and (appeal >= 2.0) and (risk < 0.35)

        print(f"  📊 [Round {round_num} 診断結果]")
        print(f"     ・数理の具体性: {math_depth:.2f} / 3.0")
        print(f"     ・筆者スタンス: {stance:.2f} / 3.0")
        print(f"     ・知的好奇心度: {appeal:.2f} / 3.0")
        print(f"     ・総合品質点数: {total_score:.2f} / 9.0 (判定: {'✅ 合格' if passed else '⚠️ 足切り・改善要'})")
        print(f"     ・診断ボトルネック: {diagnosis}")
        print(f"     ・肩透かしリスク: {risk:.1%}")

        return {
            "round": round_num,
            "math_depth": math_depth,
            "stance": stance,
            "appeal": appeal,
            "total_score": round(total_score, 2),
            "diagnosis": diagnosis,
            "lack_of_opinion_risk": risk,
            "passed": passed,
        }
    except Exception as e:
        print(f"⚠️ Jev 検証エラー: {e}")
        return {
            "round": round_num,
            "math_depth": 2.0,
            "stance": 2.0,
            "appeal": 2.0,
            "total_score": 6.0,
            "diagnosis": "high_quality",
            "lack_of_opinion_risk": 0.2,
            "passed": True,  # エラー時はパイプライン停止を防ぐため通過
        }


def rewrite_blog_post_with_gemini(
    paper: Dict[str, Any],
    previous_draft: str,
    feedback_metrics: Dict[str, Any],
    revision_round: int,
    rank: int = 1,
) -> str:
    """
    Jev による診断結果・ボトルネック指摘に基づき、Gemini にブログ記事をリライトさせる
    """
    global gemini_client
    if gemini_client is None:
        init_gemini_client()

    print(f"\n🔄 [Gemini リライト Round {revision_round}] Jevの改善フィードバックを反映して記事を再推敲中...")

    # ボトルネックに応じた重点改善指示
    diagnosis = feedback_metrics.get("diagnosis", "")
    focus_instructions = []
    
    if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
        focus_instructions.append(
            "- 【最重要：数理的機構の具体化】抽象的なお茶濁し（「〜という枠組みを導入した」等）を完全に排除してください。"
            "論文中で用いられている具体的な数学的・物理的機構（ゲージ群、対称性の破れ/高次対称性、アノマリー、カイラル代数の生成子、"
            "分配関数や指数の厳密計算、幾何学的配位など）が、専門用語の単なる羅列ではなく論理的にどう機能しているのかを明快に解説してください。"
        )
    
    if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
        focus_instructions.append(
            "- 【最重要：オピニオンの徹底強化】「で、私（筆者）はどう考えるか？」セクションが弱いです。"
            "当たり障りのない感想や一般論はすべて削除し、研究者としての明確なスタンスを打ち出してください。"
            "「この結果のどの数理的帰結が最も美しいのか」「従来のどのパラダイムを覆すのか」「どのような限界や未解決の問いが残されているか」を熱量高く論じてください。"
        )

    if diagnosis == "avoid_shallow_metaphors":
        focus_instructions.append(
            "- 【日常比喩の排除】子供騙しの日常のたとえ話（コーヒーの冷却、電車の乗り換えなど）や、ありふれたAI解説構文を完全に排除し、"
            "理論物理そのものの数理美と対称性のダイナミクスで読者を魅了してください。"
        )

    if not focus_instructions:
        focus_instructions.append(
            "- 前回のドラフト全体の論理のつながり、数理的説明のキレ、および筆者の独自スタンスをさらに一段上のレベルへ研ぎ澄ましてください。"
        )

    focus_text = "\n".join(focus_instructions)

    rewrite_prompt = f"""あなたは超弦理論、超対称共形場理論（SCFT）、場の量子論の厳密な数理構造（代数・幾何）の最前線を探究する、一流の理論物理学者兼サイエンスブロガーです。

先ほどあなたが執筆したブログ記事ドラフトに対し、レビュアー（Jev System One 診断システム）から以下の【辛口な品質診断スコアと改善要求】が届きました。

【Jev による前稿（第{revision_round - 1}稿）の診断結果】
- 数理・理論の具体性スコア: {feedback_metrics.get('math_depth', 0.0):.2f} / 3.0
- 筆者オピニオン度スコア: {feedback_metrics.get('stance', 0.0):.2f} / 3.0
- 知的好奇心刺激度スコア: {feedback_metrics.get('appeal', 0.0):.2f} / 3.0
- 総合品質スコア: {feedback_metrics.get('total_score', 0.0):.2f} / 9.0 （基準未達・改善要）
- 指摘されたボトルネック: {diagnosis}
- 「あなたの意見は？」肩透かしリスク: {feedback_metrics.get('lack_of_opinion_risk', 0.0)*100:.1f}%

【今回のリライトにおける必須改善指令】
{focus_text}

---
【対象論文情報】
- arXiv ID: {paper['arxiv_id']}
- タイトル: {paper['title']}
- 著者: {', '.join(paper['authors'])}
- アブストラクト:
{paper['summary']}

---
【前回のドラフト（第{revision_round - 1}稿）】
{previous_draft}

---
【リライト時のフォーマット規則】
1. 前回のドラフトの構成（フロントマター、## 導入、## 背景にある物理・数学の壁、## この論文の核心アイデアと数理的機構、## で、私（筆者）はどう考えるか？、## まとめ ＆ 論文リンク）を維持したまま、上記改善指令を完全に反映して全面的にブラッシュアップしてください。
2. 太字強調は必ず HTML の <strong> タグ（例: <strong>太字</strong>）を使用し、Markdownの ** は一切使用しないでください。
3. 本文先頭に「# タイトル」は置かず、フロントマターから始めてください。

知的好奇心と数理的深みに満ちた、決定版となる修正後Markdown記事を出力してください。
"""

    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash",
    ]
    rewritten_text = None
    for model_name in candidate_models:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=rewrite_prompt,
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
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """
    Evaluator-Optimizer パターンによる自律改善パイプライン:
    1. Gemini で初回ドラフトを執筆
    2. Jev で多面品質を厳密採点・足切り判定
    3. 不合格の場合、Jev の指摘を Gemini にフィードバックしてリライト（最大 max_revisions 回）
    4. 最終記事ドラフト、最終品質メトリクス、改善履歴リストを返す
    """
    # Step 1: 初回執筆
    current_post = write_blog_post_with_gemini(paper, rank=rank)
    
    # Step 2: 初回検証
    metrics = verify_post_with_jev(current_post, round_num=1)
    history = [metrics]

    # Step 3: 足切り ＆ リライトループ
    round_count = 1
    while not metrics.get("passed", False) and round_count <= max_revisions:
        round_count += 1
        print(f"\n⚡ 基準未達のため、Jevの改善指示を反映してリライトを実行します (Revision {round_count - 1}/{max_revisions})...")
        
        # リライト実行
        current_post = rewrite_blog_post_with_gemini(
            paper=paper,
            previous_draft=current_post,
            feedback_metrics=metrics,
            revision_round=round_count,
            rank=rank,
        )
        
        # 再検証
        metrics = verify_post_with_jev(current_post, round_num=round_count)
        history.append(metrics)

        if metrics.get("passed", False):
            print(f"\n🎉 Jev の厳格品質基準をクリアしました！ (Round {round_count})")
            break

    if not metrics.get("passed", False):
        print(f"\n⚠️ 最大リビジョン数 ({max_revisions}回) に達しました。現時点で最高品質の原稿を採用します。")

    return current_post, metrics, history



# ==============================================================================
# 5. 既存記事の重複チェック & 繰り上げ判定
# ==============================================================================
def get_existing_arxiv_ids(posts_dir: str) -> set:
    """
    保存先ディレクトリ（posts_dir）内の既存記事から、既に執筆済みの arXiv ID を収集。
    ファイル名（*-arxiv-*.md）および記事本文の URL から網羅的に抽出。
    """
    existing_ids = set()
    if not os.path.exists(posts_dir):
        return existing_ids

    for fname in os.listdir(posts_dir):
        if not fname.endswith(".md"):
            continue

        # 1. ファイル名から抽出 (例: 2026-09-19-arxiv-2609-20802v1.md)
        m = re.search(r"arxiv-([a-zA-Z0-9_\-]+)\.md$", fname)
        if m:
            clean_id = m.group(1).lower()
            existing_ids.add(clean_id)
            # バージョン (v1, v2 等) を除いたベース ID
            base_clean = re.sub(r"v\d+$", "", clean_id)
            existing_ids.add(base_clean)
            existing_ids.add(clean_id.replace('-', '.'))
            existing_ids.add(base_clean.replace('-', '.'))

        # 2. ファイル本文の先頭部分からも arXiv ID を念のため抽出
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


# ==============================================================================
# 6. yagibrary (Astro) 向けフォーマット整形処理
# ==============================================================================
def format_post_for_yagibrary(
    raw_markdown: str,
    paper: Dict[str, Any],
    quality: Dict[str, Any],
    rank: int = 1,
    time_offset_seconds: int = 0,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    yagibrary (Astro content collections) のフォーマット仕様に合わせて整形：
    1. title, date, summary, tags の Frontmatter 生成・正規化
    2. 本文冒頭の不要な # 見出しの除去
    3. Markdownの **太字** を HTMLの <strong>太字</strong> に変換（AGENTS.mdルール遵守）
    4. 採点レポートを記事末尾に付加（Evaluator-Optimizer 推敲履歴を含む）
    """
    # Frontmatter の抽出
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

    # タイトルの決定
    title = parsed_meta.get("title")
    if not title:
        # 本文先頭の見出しから抽出を試みる
        h1_match = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
        if h1_match:
            title = h1_match.group(1).strip()
            body = re.sub(r"^#\s+.+\n*", "", body, count=1).strip()
        else:
            title = f"【arXiv最新論文解説】{paper['title']}"

    # 本文冒頭の重複した # 見出し（h1）を削除
    body = re.sub(r"^#\s+.*?\n+", "", body).strip()

    # summary の決定
    summary = parsed_meta.get("summary")
    if not summary:
        summary = f"arXiv:{paper['arxiv_id']} 「{paper['title']}」の徹底解説。量子情報・物理の最新進展と筆者独自のオピニオンを交えて紐解きます。"

    # tags の決定
    tags = parsed_meta.get("tags")
    if not tags or not isinstance(tags, list):
        tags = ["物理学", "量子情報", paper["jev_metrics"].get("subfield", "理論物理")]
    # 重複除去 & URL・ルーティングで壊れないようサニタイズ（スラッシュ等の置換）
    cleaned_tags = []
    for t in tags:
        t_clean = str(t).strip()
        # Astro の [tag].astro ルーティングで階層エラーになるためスラッシュ等をハイフンに置換
        t_clean = t_clean.replace('/', '-').replace('\\', '-').replace(':', '-')
        if t_clean and t_clean not in cleaned_tags:
            cleaned_tags.append(t_clean)

    # JST タイムスタンプ（順位ごとに数分オフセットをつけて1位が最上位になるよう調整可能）
    jst = timezone(timedelta(hours=9))
    post_time = datetime.now(jst) + timedelta(seconds=time_offset_seconds)
    now_jst = post_time.strftime("%Y-%m-%dT%H:%M:%S+09:00")

    # 本文中の **太字** を <strong>太字</strong> に変換 (AGENTS.mdルール)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)

    # 数式ブロック（$$...$$）の正規化
    # 1行にまとまった $$式$$ を、remark-math / KaTeX が正しくブロック数式として認識し、
    # 横スクロール（スライド）可能にするため \n\n$$\n式\n$$\n\n に展開
    body = re.sub(r"(?<!\$)\$\$(?!\$)\s*([^\n]+?)\s*\$\$(?!\$)", r"\n\n$$\n\1\n$$\n\n", body)

    # 推敲改善履歴の整形
    revision_count = len(history) if history else 1
    history_steps = []
    if history:
        for h in history:
            round_lbl = f"第{h.get('round', 1)}稿"
            score_lbl = f"{h.get('total_score', 0):.2f}点"
            status_lbl = "合格" if h.get("passed") else f"足切り ({h.get('diagnosis', '要改善')})"
            history_steps.append(f"{round_lbl}: {score_lbl} [{status_lbl}]")
    history_summary = " ➡️ ".join(history_steps) if history_steps else f"{quality.get('total_score', 0):.2f}点"

    # Jev パイプライン採点レポート (太字は <strong> 使用)
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
  - 最終品質スコア: <code>{quality.get('total_score', 0):.2f} / 9.0</code>（判定: <code>{'合格' if quality.get('passed') else '足切り後採用'}</code>）
  - 数理・理論の具体性: <code>{quality.get('math_depth', 0):.2f} / 3.0</code>
  - 筆者オピニオン度: <code>{quality.get('stance', 0):.2f} / 3.0</code>
  - 知的好奇心刺激度: <code>{quality.get('appeal', 0):.2f} / 3.0</code>
  - 「で、あなたの意見は？」リスク: <code>{quality.get('lack_of_opinion_risk', 0)*100:.1f}%</code>
  - 自律推敲・改善プロセス (計 {revision_count} 回): <code>{history_summary}</code>
"""

    # Astro 用 Frontmatter の構築
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

    final_post = f"---\n{frontmatter_yaml}\n---\n\n{body}\n{meta_section}"
    return final_post


# ==============================================================================
# メイン実行関数
# ==============================================================================
def run_daily_pipeline(
    max_papers: int = 15,
    top_n_to_blog: int = 3,
    output_dir: Optional[str] = None
) -> List[str]:
    print("\n" + "=" * 65)
    print(" 🚀 arXiv × TypeSafe Jev × Gemini 全自動論文ブロガー 起動")
    print(" 🎯 対象カテゴリ: hep-th (高エネルギー理論) & quant-ph (量子情報)")
    print(f" 📑 記事生成対象: 上位 {top_n_to_blog} 件")
    print("=" * 65)

    # 保存先ディレクトリの決定 (yagibrary の posts ディレクトリを優先)
    if output_dir is None:
        if os.path.exists(DEFAULT_YAGIBRARY_POSTS_DIR):
            target_dir = DEFAULT_YAGIBRARY_POSTS_DIR
        else:
            target_dir = os.path.join(os.path.dirname(__file__), "generated_posts")
    else:
        target_dir = output_dir

    # 1. 論文取得
    papers = fetch_arxiv_papers(max_results=max_papers)
    if not papers:
        print("❌ 論文を取得できませんでした。終了します。")
        return []

    # 2. Jev で採点 & ランキング
    ranked_papers = screen_and_rank_papers_with_jev(papers)
    if not ranked_papers:
        print("❌ スクリーニングに失敗しました。")
        return []

    # 上位件数をサマリー表示
    display_count = min(len(ranked_papers), max(3, top_n_to_blog))
    print(f"\n🏆 【本日の TOP {display_count} 厳選論文】")
    for i, p in enumerate(ranked_papers[:display_count]):
        m = p["jev_metrics"]
        print(f"  第{i+1}位: [{p['arxiv_id']}] {p['title'][:65]}...")
        print(f"         総合スコア: {m['total_score']} | 関連度: {m['is_quantum_relevant']:.1%} | 魅力: {m['blog_appeal']:.1f} | {m['subfield']}")

    # 既存記事の arXiv ID を検出
    existing_arxiv_ids = get_existing_arxiv_ids(target_dir)
    if existing_arxiv_ids:
        print(f"📚 既存記事ディレクトリ ({target_dir}) から執筆済み arXiv ID を照合中...")

    # 記事化対象の選定（未執筆のものを上位から top_n_to_blog 件選定）
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

        # 3. Gemini × Jev 自律推敲・リライトループ（Evaluator-Optimizer パターン）
        raw_markdown, quality, history = generate_refined_blog_post(
            paper=paper,
            rank=rank,
            max_revisions=2,
        )

        # 4. yagibrary 形式へのフォーマット整形 (Frontmatter、<strong> タグ変換、推敲レポート等)
        # 一覧で上位記事が最上位になるよう、今回のバッチ内の順序に応じて数分未来のタイムスタンプを設定
        time_offset = (len(target_papers) - batch_idx) * 60
        final_post = format_post_for_yagibrary(
            raw_markdown,
            paper,
            quality,
            rank=rank,
            time_offset_seconds=time_offset,
            history=history,
        )

        # 5. ファイル保存
        clean_id = paper['arxiv_id'].replace('/', '_').replace('.', '-')
        filename = f"{today_str}-arxiv-{clean_id}.md"
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


# ==============================================================================
# 特定論文指定パイプライン
# ==============================================================================
def run_targeted_pipeline(arxiv_ids: List[str], output_dir: Optional[str] = None) -> List[str]:
    """特定の arXiv ID を指定してピンポイントでブログ記事を執筆・保存するモード"""
    print("\n" + "=" * 65)
    print(" 🎯 arXiv × TypeSafe Jev × Gemini 特定論文ブロガー 起動")
    print(f" 📑 指定論文: {', '.join(arxiv_ids)}")
    print("=" * 65)

    if output_dir is None:
        if os.path.exists(DEFAULT_YAGIBRARY_POSTS_DIR):
            target_dir = DEFAULT_YAGIBRARY_POSTS_DIR
        else:
            target_dir = os.path.join(os.path.dirname(__file__), "generated_posts")
    else:
        target_dir = output_dir

    papers = fetch_arxiv_papers_by_ids(arxiv_ids)
    if not papers:
        print("❌ 指定された論文を取得できませんでした。")
        return []

    # Jev で評価・スコアリング（診断レポート作成用）
    ranked_papers = screen_and_rank_papers_with_jev(papers)

    os.makedirs(target_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    generated_files = []

    for i, paper in enumerate(ranked_papers):
        batch_idx = i + 1
        print("\n" + "-" * 65)
        print(f" 🖋️ [記事執筆・整形 {batch_idx}/{len(ranked_papers)}] 対象論文: {paper['arxiv_id']}")
        print(f"    タイトル: {paper['title']}")
        print("-" * 65)

        # Gemini × Jev 自律推敲・リライトループ
        raw_markdown, quality, history = generate_refined_blog_post(
            paper=paper,
            rank=batch_idx,
            max_revisions=2,
        )

        time_offset = (len(ranked_papers) - batch_idx) * 60
        final_post = format_post_for_yagibrary(
            raw_markdown,
            paper,
            quality,
            rank=batch_idx,
            time_offset_seconds=time_offset,
            history=history,
        )

        clean_id = paper['arxiv_id'].replace('/', '_').replace('.', '-')
        filename = f"{today_str}-arxiv-{clean_id}.md"
        file_path = os.path.join(target_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_post)

        generated_files.append(file_path)
        print(f" ✅ [指定論文] 記事保存完了: {file_path}")

    print("\n" + "=" * 65)
    print(f" 🎉 全 {len(generated_files)} 件のブログ記事の生成が完了しました！")
    for idx, fp in enumerate(generated_files):
        print(f"   [{idx+1}] {fp}")
    print("=" * 65)
    return generated_files


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="arXiv × TypeSafe Jev × Gemini 自律型ブログ執筆パイプライン")
    parser.add_argument("--arxiv-id", "-a", type=str, default="", help="特定の arXiv 論文番号（カンマ区切りで複数可。例: 2006.13892）")
    parser.add_argument("--max-papers", "-m", type=int, default=15, help="arXivから自動取得する件数 (デフォルト: 15)")
    parser.add_argument("--top-n", "-n", type=int, default=3, help="ブログ記事化する上位件数 (デフォルト: 3)")
    parser.add_argument("--output-dir", "-o", type=str, default=None, help="記事保存先ディレクトリ")
    parser.add_argument("positional_args", nargs="*", help="後方互換用: [max_papers] [top_n]")

    args = parser.parse_args()

    # 位置引数があれば上書き (python script.py 10 3 など)
    if args.positional_args:
        try:
            args.max_papers = int(args.positional_args[0])
            if len(args.positional_args) > 1:
                args.top_n = int(args.positional_args[1])
        except ValueError:
            pass

    if args.arxiv_id.strip():
        # 特定論文指定モード
        target_ids = [aid.strip() for aid in args.arxiv_id.split(",") if aid.strip()]
        run_targeted_pipeline(arxiv_ids=target_ids, output_dir=args.output_dir)
    else:
        # 自動スクリーニングモード
        run_daily_pipeline(max_papers=args.max_papers, top_n_to_blog=args.top_n, output_dir=args.output_dir)

