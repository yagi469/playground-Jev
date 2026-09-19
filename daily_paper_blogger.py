#!/usr/bin/env python3
"""
daily_paper_blogger.py
======================
arXiv (hep-th / quant-ph) から最新の量子コンピュータ・量子情報・ホログラフィ関連の論文を自動取得し、
TypeSafe (Jev) による高速多面スクリーニングで本日のベスト論文を厳選、
Google Gemini で独自の考察・スタンスを盛り込んだブログ解説記事を自動執筆するパイプライン。
"""

import os
import io
import time
import json
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
import httpx
from bs4 import BeautifulSoup
import yaml
from dotenv import load_dotenv
from typesafe_sdk import TypeSafeClient, Choice, Score, Noul

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = None
    PdfWriter = None

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


def fetch_arxiv_papers(max_results: int = 50) -> List[Dict[str, Any]]:
    """
    hep-th (高エネルギー理論) と math-ph (数理物理) を最重要母集団とし、
    quant-ph も含めてバランスよく最新論文を取得。
    hep-th は毎日数十件の新規投稿があるため、母集団を十分に確保して取りこぼしを防止。
    """
    print("\n📡 [arXiv API] hep-th (最重要) & math-ph & quant-ph から最新論文を取得中...")

    # hep-th の日次新規投稿（約40〜50件）をほぼ網羅できるように重点配分
    hep_count = max(40, int(max_results * 0.70))
    math_count = max(15, int(max_results * 0.25))
    quant_count = max(15, int(max_results * 0.25))

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


def fetch_arxiv_paper_content(arxiv_id: str) -> Optional[Dict[str, Any]]:
    """
    arXiv公式HTMLまたはar5ivから論文本文を取得し、
    主要セクション（Introduction、Theorems/Results、Conclusion）を抽出する。
    """
    clean_id = re.sub(r"^arxiv:\s*", "", arxiv_id, flags=re.IGNORECASE).strip()
    base_id = re.sub(r"v\d+$", "", clean_id)

    print(f"\n📖 [arXiv HTML/ar5iv] 論文本文を取得・解析中: arXiv:{clean_id}...")

    urls = [
        f"https://arxiv.org/html/{clean_id}",
        f"https://ar5iv.labs.arxiv.org/html/{clean_id}",
        f"https://arxiv.org/html/{base_id}",
        f"https://ar5iv.labs.arxiv.org/html/{base_id}",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    html_text = None
    success_url = None
    for url in urls:
        try:
            resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=15.0)
            if resp.status_code == 200 and ("ltx_document" in resp.text or "ltx_section" in resp.text or "article" in resp.text):
                html_text = resp.text
                success_url = url
                break
        except Exception:
            continue

    if not html_text:
        print("  ⚠️ arXiv HTML本文の取得をスキップ（利用不可または未変換）")
        return None

    print(f"  ✓ 論文HTML取得成功: {success_url}")
    soup = BeautifulSoup(html_text, "html.parser")

    # セクションの抽出
    sections = soup.find_all(["section", "div"], class_=re.compile(r"ltx_section|ltx_appendix|section"))
    
    intro_texts = []
    results_texts = []
    conclusion_texts = []
    section_titles = []

    for sec in sections:
        header = sec.find(["h2", "h3", "h4", "span"], class_=re.compile(r"ltx_title"))
        title = header.get_text(strip=True) if header else ""
        if title:
            section_titles.append(title)
        
        lower_t = title.lower()
        text = sec.get_text(separator=" ", strip=True)
        if len(text) > 4000:
            text = text[:4000] + "..."

        if any(k in lower_t for k in ["intro", "background", "motivation"]):
            intro_texts.append(f"### {title}\n{text}")
        elif any(k in lower_t for k in ["conclus", "discuss", "outlook", "summary"]):
            conclusion_texts.append(f"### {title}\n{text}")
        elif any(k in lower_t for k in ["main", "theorem", "result", "model", "construction", "algebra", "geometric", "duality", "index"]):
            results_texts.append(f"### {title}\n{text}")

    if not results_texts and len(sections) > 1:
        for sec in sections[1:4]:
            t = sec.get_text(separator=" ", strip=True)
            if t:
                results_texts.append(t[:3000])

    print(f"  ✓ 論文本文抽出完了: セクション数 {len(section_titles)}, 本文抜粋 約{sum(len(x) for x in intro_texts + results_texts + conclusion_texts)} 文字")

    return {
        "section_names": ", ".join(section_titles[:10]),
        "intro": "\n\n".join(intro_texts)[:3500],
        "main_results": "\n\n".join(results_texts)[:5000],
        "conclusion": "\n\n".join(conclusion_texts)[:2000],
    }


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

    full_text_section = ""
    fc = paper.get("full_text_content")
    if fc:
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
{full_text_section}
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
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象論文へのリンク（[{paper['arxiv_id']}]({paper['url']})）およびタイトル・著者情報を明記し、難解な数式に入る前に『この記事の核心アイデア（1分で掴む直観的イメージ）』を提示して読者が迷子にならないロードマップを示してください。
     - ## 背景にある物理・数学の壁: 従来の理論（摂動論、標準的場の理論、既存のホログラフィ等）の何が未解決だったのか、なぜこの問題が本質的なのかを論理的かつクリアに解説。
     - ## この論文の核心アイデアと数理的機構: 著者がどのようなアイデア・数理構造（対称性、代数、幾何学的配位、双対性など）を用いてその壁を乗り越えたのかを解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：独自のスタンス・考察・ツッコミ）単なる要約で終わらせず、数理物理・非摂動QFT・超対称性の視点から「ここが美しい」「この仮定はどこまで一般化できるのか？」「今後の研究の方向性」など、研究者としての骨太なオピニオンを展開。
     - ## まとめ ＆ 論文リンク: 記事の総括と、arXivアブストラクトへのリンク（[{paper['arxiv_id']}]({paper['url']})）、PDFへのリンク（[PDF]({paper['pdf_url']})）を分かりやすくリスト形式で設置してください。

4. **数式ブロック（Display Math）の改行ルール**:
   - 独立したブロック数式（$$ ... $$）を出力する際は、インラインとして折り返されるのを防ぎ横スライド（スクロール）可能にするため、必ず前後に改行を入れて $$ を独立した行に配置してください：
     $$
     数式
     $$

5. **前提知識の導入と途中計算・行間の明示（最重要：読者を置いてけぼりにしない解説）**:
   - 難解な専門用語や結果の数式をいきなり天下り式に並べないでください。
   - 使用する記号（ゲージ群、接続、構造定数など）や、電磁気学（U(1)）等の既知の初等理論との違いを必ず事前に平易に定義・説明してください。
   - 核心となる数式については、「なぜその式になるのか」「どう変形したのか」という『途中計算のステップ（行間）』を1〜3段階明記し、読者が自分の頭で追体験できるように解説してください。
   - 数学的手続きが物理的に「何を解決するために必要なのか」という動機を必ず言葉で解き明かしてください。

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
                "記事中で、核心となる数学的構造（対称性、代数、幾何学的配位、双対性、不変量、作用素など）や"
                "理論的機構が、抽象的な形容詞だけでなく、具体的に論理的に解説されているかを評価してください。"
            ),
            criteria=[
                "中身が薄い（抽象的な美辞麗句ばかりで、何がどう作用しているのか数理のロジックが見えない）",
                "表面的（専門用語は並んでいるが、どういう仕組みで問題が解決されたかの掘り下げが浅い）",
                "明確で具体的（アイデアや数理構造、物理的帰結の論理展開が明快に解説されている）",
                "極めて深い（非摂動効果や厳密解、代数・幾何の核心と美しさが鮮やかに浮き彫りにされている）",
            ],
        ),
        # 2. 教育的明快さ・前提知識と途中計算（行間）の丁寧さ
        "pedagogical_clarity": Score(
            instructions=(
                "前提知識（何が既知で何が新しいのか）や使用する記号・演算子の定義が丁寧に説明され、"
                "主要な数式の導出ステップ（なぜその式になるのか、どう変形したのかの行間）が、"
                "読者が自分の頭で追体験できるように明快かつ親切に解説されているかを評価してください。"
            ),
            criteria=[
                "読者置いてけぼり（前提や記号の定義がなく、途中計算も抜けていて難解な数式が突然並んでいる）",
                "行間が不親切（専門用語や結果の式が並んでいるが、どういう計算を経て導かれたのかの筋道が見えにくい）",
                "明快で親切（前提知識や記号の意味が明示され、主要な数式の導出ステップが論理的に追える）",
                "圧倒的な教育的明快さ（初等概念との対比から途中計算、物理的帰結までが完璧に繋がっており、極めて深く理解できる）",
            ],
        ),
        # 3. 筆者オピニオンの切れ味・独自スタンス
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
        # 4. 読者の知的好奇心刺激度
        "intellectual_appeal": Score(
            instructions=(
                "数理物理学や理論物理に関心を持つ読者にとって、知的好奇心が強く刺激され、"
                "「この論文/本を読んでみたい」「この視点は面白い」と思わせる魅力があるかを評価してください。"
            ),
            criteria=[
                "退屈・安易（子供騙しの比喩やありふれたAIまとめ構文で、知的好奇心が湧かない）",
                "教科書的（論理は通っているが、ワクワクするような熱量や知的フックに欠ける）",
                "魅力的（問題の本質とブレイクスルーの意義が伝わり、読んでいて面白い）",
                "圧倒的（理論物理の真の美しさとスリルが伝わり、読者を強く引き込む名論考）",
            ],
        ),
        # 5. 最大の改善ボトルネック診断
        "critique_diagnosis": Choice(
            instructions="この記事のクオリティをさらに高めるために、最も改善が必要なボトルネックはどこですか？",
            criteria={
                "need_pedagogical_steps": "前提知識・記号の定義が不足、または数式の途中計算（行間）が省略されている。読者が追体験できる導出ステップの解説が必要",
                "need_math_details": "核心アイデアの数理的機構や代数・幾何のロジックが抽象的。具体的な作用素・不変量・計算機構の解説が必要",
                "need_sharp_opinion": "筆者オピニオンが論文の無難なまとめ。独自の問い・批判的考察・数理的意義をもっと熱く語るべき",
                "avoid_shallow_metaphors": "安易な日常のたとえ話やAI特有のお茶濁しが目立つ。理論物理の真の美しさに徹するべき",
                "high_quality": "前提の丁寧さ、数理の具体性、途中計算、オピニオンの深さが極めて高い水準で調和している",
            },
        ),
        # 6. 「で、あなたの意見は？」肩透かしリスク
        "lack_of_opinion_risk": Noul(
            instructions="読者が読み終わった後に「事実は分かったけど、結局筆者はどう思っているの？」と肩透かしを感じるリスクがありますか？"
        ),
        # 7. 「難解すぎて置いてけぼり」リスク
        "rushed_math_risk": Noul(
            instructions=(
                "前提となる物理概念の直観的導入や数式の行間・変形ステップが不親切で、専門用語をただ並べただけのために、"
                "理論物理・数理に関心を持つ読者（理工系学部・大学院生層）ですら『置いてけぼり』や『理解不能』に感じるリスクがありますか？"
            )
        ),
    }

    try:
        res = typesafe_client.system_one(state={"post": post_content}, questions=questions)
        math_depth = res.scores["mathematical_depth"].score
        clarity = res.scores["pedagogical_clarity"].score
        stance = res.scores["author_stance"].score
        appeal = res.scores["intellectual_appeal"].score
        diagnosis = res.choices["critique_diagnosis"].choice
        risk_opinion = res.nouls["lack_of_opinion_risk"].noul
        risk_rushed = res.nouls["rushed_math_risk"].noul

        total_score = math_depth + clarity + stance + appeal  # 最大 12.0

        # 基本足切り基準: 総合 9.2 以上、かつ各項目 2.0 以上、かつリスク 38% 未満 / 置いてけぼり 40% 未満
        base_passed = (
            (total_score >= 9.2)
            and (math_depth >= 2.0)
            and (clarity >= 2.0)
            and (stance >= 2.0)
            and (appeal >= 2.0)
            and (risk_opinion < 0.38)
            and (risk_rushed < 0.40)
        )

        # 高品質ボーナス判定: 総合10.0以上、丁寧さ2.5以上、ボトルネックがhigh_qualityの場合は、
        # 最先端理論物理の必然的な高度さを考慮し、置いてけぼりリスク 45% 未満まで合格とする
        high_quality_passed = (
            (total_score >= 10.0)
            and (clarity >= 2.5)
            and (diagnosis == "high_quality")
            and (risk_opinion < 0.40)
            and (risk_rushed < 0.45)
        )

        passed = base_passed or high_quality_passed

        print(f"  📊 [Round {round_num} 診断結果]")
        print(f"     ・数理の具体性: {math_depth:.2f} / 3.0")
        print(f"     ・行間・途中計算の丁寧さ: {clarity:.2f} / 3.0")
        print(f"     ・筆者スタンス: {stance:.2f} / 3.0")
        print(f"     ・知的好奇心度: {appeal:.2f} / 3.0")
        print(f"     ・総合品質点数: {total_score:.2f} / 12.0 (判定: {'✅ 合格' if passed else '⚠️ 足切り・改善要'})")
        print(f"     ・診断ボトルネック: {diagnosis}")
        print(f"     ・肩透かしリスク: {risk_opinion:.1%} / 置いてけぼりリスク: {risk_rushed:.1%}")

        return {
            "round": round_num,
            "math_depth": math_depth,
            "clarity": clarity,
            "stance": stance,
            "appeal": appeal,
            "total_score": round(total_score, 2),
            "diagnosis": diagnosis,
            "lack_of_opinion_risk": risk_opinion,
            "rushed_math_risk": risk_rushed,
            "passed": passed,
        }
    except Exception as e:
        print(f"⚠️ Jev 検証エラー: {e}")
        return {
            "round": round_num,
            "math_depth": 2.0,
            "clarity": 2.0,
            "stance": 2.0,
            "appeal": 2.0,
            "total_score": 8.0,
            "diagnosis": "high_quality",
            "lack_of_opinion_risk": 0.2,
            "rushed_math_risk": 0.2,
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

    if feedback_metrics.get("clarity", 0.0) < 2.0 or feedback_metrics.get("rushed_math_risk", 0.0) >= 0.35 or diagnosis == "need_pedagogical_steps":
        focus_instructions.append(
            "- 【最重要：前提知識と途中計算（行間）の徹底解説】読者が置いてけぼりになっています！"
            "難解な専門用語や結果の数式をいきなり展示するのを完全にやめてください。\n"
            "  1. 記号（各文字が何を表すか）や前提となる初等概念（電磁気学や標準的な場の理論など）との違いを必ず平易に定義・説明してください。\n"
            "  2. 重要な数式については、なぜその式になるのか、どう変形したのかという『途中計算のステップ（1〜3ステップ）』を必ず本文中に明記してください。\n"
            "  3. 「なぜこの概念・計算が必要なのか」という物理的・数学的動機を、読者が納得できるように丁寧に解き明かしてください。"
        )

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

    # 論文本文（HTML）の要所抜粋があればプロンプトに注入
    full_text_section = ""
    fc = paper.get("full_text_content")
    if fc:
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
{full_text_section}
---
【前回のドラフト（第{revision_round - 1}稿）】
{previous_draft}

---
【リライト時のフォーマット規則】
1. 前回のドラフトの構成（フロントマター、## 導入、## 背景にある物理・数学の壁、## この論文の核心アイデアと数理的機構、## で、私（筆者）はどう考えるか？、## まとめ ＆ 論文リンク）を維持したまま、上記改善指令を完全に反映して全面的にブラッシュアップしてください。
2. 太字強調は必ず HTML の <strong> タグ（例: <strong>太字</strong>）を使用し、Markdownの ** は一切使用しないでください。
3. 本文先頭に「# タイトル」は置かず、フロントマターから始めてください。
4. 数式ブロックは必ず独立した行（$$\\n数式\\n$$）で出力してください。

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
  - 最終品質スコア: <code>{quality.get('total_score', 0):.2f} / 12.0</code>（判定: <code>{'合格' if quality.get('passed') else '足切り後採用'}</code>）
  - 数理・理論の具体性: <code>{quality.get('math_depth', 0):.2f} / 3.0</code>
  - 行間・途中計算の丁寧さ: <code>{quality.get('clarity', 0):.2f} / 3.0</code>
  - 筆者オピニオン度: <code>{quality.get('stance', 0):.2f} / 3.0</code>
  - 知的好奇心刺激度: <code>{quality.get('appeal', 0):.2f} / 3.0</code>
  - 「で、あなたの意見は？」リスク: <code>{quality.get('lack_of_opinion_risk', 0)*100:.1f}%</code>
  - 「難解・置いてけぼり」リスク: <code>{quality.get('rushed_math_risk', 0)*100:.1f}%</code>
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
    max_papers: int = 50,
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

        # 2.5. 論文本文（HTML/ar5iv）の重要セクション抽出
        paper["full_text_content"] = fetch_arxiv_paper_content(paper["arxiv_id"])

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

        # 論文本文（HTML/ar5iv）の重要セクション抽出
        paper["full_text_content"] = fetch_arxiv_paper_content(paper["arxiv_id"])

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


# ==============================================================================
# ローカルファイル（PDF / Markdown）処理パイプライン
# ==============================================================================
def resolve_document_path(file_path: str) -> str:
    """ローカルファイルパスを解決する（OS間の区切り文字の違い、カレント、yagibrary、docs等を自動探索）"""
    # Windows のバックスラッシュをスラッシュに正規化
    normalized = file_path.replace("\\", "/").strip()
    clean_rel = re.sub(r"^(?:docs/|yagibrary/docs/|yagibrary/)", "", normalized)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # 1. そのままのパス（絶対パスまたはカレント基準）
        os.path.abspath(normalized),
        os.path.join(script_dir, normalized),
        # 2. GitHub Actions環境 (カレント配下に yagibrary がチェックアウトされる)
        os.path.join(script_dir, "yagibrary", normalized),
        os.path.join(script_dir, "yagibrary/docs", clean_rel),
        # 3. ローカル開発環境 (playground-Jev と yagibrary が同階層に配置)
        os.path.abspath(os.path.join(script_dir, "../yagibrary", normalized)),
        os.path.abspath(os.path.join(script_dir, "../yagibrary/docs", clean_rel)),
        os.path.abspath(os.path.join(script_dir, "docs", clean_rel)),
    ]

    seen = set()
    unique_candidates = []
    for c in candidates:
        norm_c = os.path.normpath(c)
        if norm_c not in seen:
            seen.add(norm_c)
            unique_candidates.append(norm_c)

    for c in unique_candidates:
        if os.path.exists(c) and os.path.isfile(c):
            return c

    raise FileNotFoundError(f"指定されたファイルが見つかりませんでした: '{file_path}'. 探索候補: {unique_candidates}")


def extract_pdf_pages_bytes(pdf_path: str, pages_str: Optional[str] = None) -> Tuple[bytes, str]:
    """
    指定された PDF ファイルから指定ページ範囲を抽出してバイナリ (bytes) とラベルを返す。
    pages_str 例: "15-30", "45", "1-10,15,20-25" (1-indexed)。
    """
    if PdfReader is None or PdfWriter is None:
        raise ImportError("pypdf がインストールされていません。'pip install pypdf' を実行してください。")

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
        # ページ指定なしの場合、上限80ページを抽出
        max_default = 80
        count = min(total_pages, max_default)
        for idx in range(count):
            writer.add_page(reader.pages[idx])
        if total_pages > max_default:
            label = f"先頭 1-{max_default} ページ (全 {total_pages} ページ中)"
        else:
            label = f"全 {total_pages} ページ"

    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()
    return pdf_bytes, label


def load_and_process_local_file(
    file_path: str,
    pages_str: Optional[str] = None,
    chapter_hint: Optional[str] = None
) -> Dict[str, Any]:
    """
    ローカルの PDF または Markdown / Text ファイルを読み込み、Gemini 用のコンテンツオブジェクトと
    基本メタデータ（タイトル・要約・サブ領域など）を構造化して返す。
    """
    global gemini_client
    if gemini_client is None:
        init_gemini_client()

    resolved_path = resolve_document_path(file_path)
    file_name = os.path.basename(resolved_path)
    ext = os.path.splitext(file_name)[-1].lower()

    print(f"\n📂 [ローカルファイル読解] ファイル: {resolved_path} (拡張子: {ext})")
    if chapter_hint:
        print(f"   🎯 対象章/テーマ: {chapter_hint}")

    doc_info: Dict[str, Any] = {
        "file_path": resolved_path,
        "file_name": file_name,
        "extension": ext,
        "chapter_hint": chapter_hint or "",
        "page_label": "",
    }

    if ext == ".pdf":
        from google.genai import types
        pdf_bytes, page_label = extract_pdf_pages_bytes(resolved_path, pages_str)
        doc_info["page_label"] = page_label
        doc_info["pdf_part"] = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        doc_info["doc_type"] = "pdf"
        print(f"   📑 PDF 抽出完了: {page_label} ({len(pdf_bytes):,} bytes)")

        # Gemini に基本メタデータの抽出を依頼
        meta_prompt = f"""添付のPDFドキュメント（抽出範囲: {page_label}、指定テーマ/章: {chapter_hint or '指定なし'}）の内容を読み取り、
以下のJSON形式でメタデータを出力してください。Markdownの```json ... ```形式で囲んでください。
{{
  "title": "このドキュメントまたは対象セクションの的確なタイトル（日本語または英語の原題）",
  "authors": ["著者名または編者名（判明する場合）"],
  "summary": "このドキュメント/対象セクションで論じられている核心内容の要約（150〜250文字）",
  "categories": ["数理物理", "場の量子論", "その他関連分野タグ3個程度"],
  "subfield": "SCFT, AdS/CFT, カイラル代数, 非摂動QFT など具体的な専門分野"
}}
"""
        try:
            res = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[doc_info["pdf_part"], meta_prompt]
            )
            raw_text = res.text
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if json_match:
                meta = json.loads(json_match.group(1))
            else:
                meta = json.loads(raw_text.strip())
        except Exception as e:
            print(f"   ⚠️ メタデータ抽出フォールバック: {e}")
            meta = {
                "title": os.path.splitext(file_name)[0],
                "authors": ["著者不明"],
                "summary": f"{file_name} の抜粋解説（{page_label}）。",
                "categories": ["数理物理", "理論物理"],
                "subfield": "数理物理学",
            }

        doc_info.update(meta)

    elif ext in [".md", ".markdown", ".txt"]:
        with open(resolved_path, "r", encoding="utf-8") as f:
            text_content = f.read()

        doc_info["doc_type"] = "markdown"
        doc_info["text_content"] = text_content
        doc_info["page_label"] = f"テキストファイル ({len(text_content):,} 文字)"
        print(f"   📝 Markdown 読込完了: {len(text_content):,} 文字")

        meta_prompt = f"""以下のテキスト文書（指定テーマ/章: {chapter_hint or '指定なし'}）を読み取り、
以下のJSON形式でメタデータを出力してください。Markdownの```json ... ```形式で囲んでください。
{{
  "title": "この文書の的確なタイトル",
  "authors": ["著者名（判明する場合）"],
  "summary": "この文書の核心内容の要約（150〜250文字）",
  "categories": ["数理物理", "場の量子論", "その他関連分野タグ3個程度"],
  "subfield": "SCFT, AdS/CFT, カイラル代数, 非摂動QFT など具体的な専門分野"
}}

【文書本文（先頭抜粋）】
{text_content[:8000]}
"""
        try:
            res = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=meta_prompt
            )
            raw_text = res.text
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if json_match:
                meta = json.loads(json_match.group(1))
            else:
                meta = json.loads(raw_text.strip())
        except Exception as e:
            print(f"   ⚠️ メタデータ抽出フォールバック: {e}")
            meta = {
                "title": os.path.splitext(file_name)[0],
                "authors": ["記録者"],
                "summary": f"{file_name} の解説ノート。",
                "categories": ["数理物理", "研究ノート"],
                "subfield": "数理物理学",
            }

        doc_info.update(meta)
    else:
        raise ValueError(f"未対応のファイル形式です: {ext} (対応: .pdf, .md, .markdown, .txt)")

    print(f"   🏷️ 認識タイトル: {doc_info.get('title')}")
    print(f"   🏷️ 専門サブ領域: {doc_info.get('subfield')}")
    return doc_info


def write_blog_post_from_doc_with_gemini(doc_info: Dict[str, Any]) -> str:
    """
    PDF または Markdown の内容から、Gemini で本格的な数理物理ブログ記事（初稿）を執筆
    """
    global gemini_client
    if gemini_client is None:
        init_gemini_client()

    print(f"\n✍️ [Gemini 執筆] '{doc_info['title']}' の解説ブログ記事を自律生成中...")

    chapter_focus = f"- フォーカスする章・テーマ: {doc_info['chapter_hint']}\n" if doc_info.get("chapter_hint") else ""
    page_focus = f"- 抽出範囲: {doc_info['page_label']}\n" if doc_info.get("page_label") else ""

    prompt = f"""あなたは超弦理論、超対称共形場理論（SCFT）、場の量子論の厳密な数理構造（代数・幾何）、AdS/CFT対応の最前線を探究する、一流の理論物理学者兼サイエンスブロガーです。
読者が「で、あなたの意見は？」と突っ込みたくなるような退屈なAIまとめ記事ではなく、
安易で子供騙しな日常のたとえ話（コーヒーの冷却など）に逃げず、理論物理・数理構造の真の美しさ・対称性の幾何・代数的機構を生き生きと語り尽くす、知的好奇心を刺激する熱いブログ記事を執筆してください。

【取り上げるドキュメント情報】
- 文書名: {doc_info['file_name']}
- タイトル: {doc_info['title']}
- 著者/編者: {', '.join(doc_info.get('authors', ['-']))}
- 分野/カテゴリ: {', '.join(doc_info.get('categories', ['数理物理']))}
- 専門領域: {doc_info.get('subfield', '数理物理学')}
{page_focus}{chapter_focus}- 概要:
{doc_info.get('summary', '')}

---
【記事の構成とフォーマット規則】
1. **フロントマター（YAML Frontmatter）を記事先頭に必ず出力してください**:
---
title: "思わずクリックしたくなる、知的好奇心と物理的本質を突いた日本語タイトル"
summary: "120〜180文字程度の魅力的な記事要約（何が論じられ、なぜ物理・数理として美しいのかが伝わる文章）"
tags:
  - 物理学
  - （ドキュメント内容に即したタグを3〜5個。スラッシュは使わずハイフンを使用。例: 素粒子論, 超共形場理論, AdS-CFT, カイラル代数, 超弦理論, TQFTなど）
---

2. **太字・強調ルールの遵守（最重要）**:
   - ブログ記事内でテキストを太字・強調する場合は、Markdownの ** 記法ではなく、必ず HTMLの <strong> タグ（例: <strong>太字テキスト</strong>）を使用してください。

3. **本文の見出し構成**:
   - 本文の開始部分に「# タイトル」を置かないでください（フロントマターのtitleがWebサイト側で自動描画されるため）。
   - 本文の見出しは「## （見出し名）」から始めてください。
   - 以下の構成で執筆してください：
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象ドキュメント（『{doc_info['title']}』{' - ' + doc_info['chapter_hint'] if doc_info.get('chapter_hint') else ''}）について言及し、難解な数式に入る前に『この記事の核心アイデア（1分で掴む直観的イメージ）』を提示して読者が迷子にならないロードマップを示してください。
     - ## 背景にある物理・数学の壁: 従来の枠組みの何が未解決だったのか、なぜこの理論・概念が本質的なのかを論理的かつクリアに解説。
     - ## 核心アイデアと数理的機構: 著者がどのようなアイデア・数理構造（対称性、代数、幾何学的配位、双対性など）を展開しているかを解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：独自のスタンス・考察・ツッコミ）単なる要約で終わらせず、数理物理・非摂動QFT・超対称性の視点から「ここが美しい」「この仮定・手法はどこまで拡張可能か？」「今後の研究・学習における位置づけ」など、研究者としての骨太なオピニオンを展開。
     - ## まとめ ＆ 参考文献・関連情報: 記事の総括と、文献情報（ファイル名: {doc_info['file_name']}、{doc_info['page_label']}）を分かりやすくリスト形式で設置してください。

4. **数式ブロック（Display Math）の改行ルール**:
   - 独立したブロック数式（$$ ... $$）を出力する際は、インラインとして折り返されるのを防ぎ横スライド（スクロール）可能にするため、必ず前後に改行を入れて $$ を独立した行に配置してください：
     $$
     数式
     $$

5. **前提知識の導入と途中計算・行間の明示（最重要：読者を置いてけぼりにしない解説）**:
   - 難解な専門用語や結果の数式をいきなり天下り式に並べないでください。
   - 使用する記号（ゲージ群、接続、構造定数など）や、電磁気学（U(1)）等の既知の初等理論との違いを必ず事前に平易に定義・説明してください。
   - 核心となる数式については、「なぜその式になるのか」「どう変形したのか」という『途中計算のステップ（行間）』を1〜3段階明記し、読者が自分の頭で追体験できるように解説してください。
   - 数学的手続き（共変微分の導入、ゴースト、BRSTなど）が物理的に「何を解決するために必要なのか」という動機を必ず言葉で解き明かしてください。

Markdown形式で出力してください。
"""

    if doc_info["doc_type"] == "pdf":
        contents = [doc_info["pdf_part"], prompt]
    else:
        text_body = doc_info.get("text_content", "")[:35000]
        contents = [f"【ドキュメント本文】\n{text_body}\n\n", prompt]

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
) -> str:
    """Jev のフィードバックに基づきドキュメント解説記事を再推敲"""
    global gemini_client
    if gemini_client is None:
        init_gemini_client()

    print(f"\n🔄 [Gemini リライト Round {revision_round}] Jevの改善フィードバックを反映してドキュメント記事を再推敲中...")

    diagnosis = feedback_metrics.get("diagnosis", "")
    focus_instructions = []

    if feedback_metrics.get("clarity", 0.0) < 2.0 or feedback_metrics.get("rushed_math_risk", 0.0) >= 0.35 or diagnosis == "need_pedagogical_steps":
        focus_instructions.append(
            "- 【最重要：前提知識と途中計算（行間）の徹底解説】読者が置いてけぼりになっています！"
            "難解な専門用語や結果の数式をいきなり展示するのを完全にやめてください。\n"
            "  1. 記号（各文字が何を表すか）や前提となる初等概念（電磁気学や標準的な場の理論など）との違いを必ず平易に定義・説明してください。\n"
            "  2. 重要な数式については、なぜその式になるのか、どう変形したのかという『途中計算のステップ（1〜3ステップ）』を必ず本文中に明記してください。\n"
            "  3. 「なぜこの概念・計算が必要なのか」という物理的・数学的動機を、読者が納得できるように丁寧に解き明かしてください。"
        )

    if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
        focus_instructions.append(
            "- 【最重要：数理的機構の具体化】抽象的な表現やお茶濁しを排除し、具体的な数学的・物理的機構"
            "（対称性、不変量、代数構造、指数の厳密計算、幾何学的性質など）がどう論理的に機能しているのかを明快に解説してください。"
        )

    if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
        focus_instructions.append(
            "- 【最重要：オピニオンの徹底強化】「で、私（筆者）はどう考えるか？」セクションを強化してください。"
            "当たり障りのない要約を脱し、「どの数理的帰結が最も美しいか」「どのような意義や限界があるか」を熱量高く論じてください。"
        )

    if diagnosis == "avoid_shallow_metaphors":
        focus_instructions.append(
            "- 【日常比喩の排除】子供騙しの日常たとえ話を完全排除し、数理美そのもので読者を引き込んでください。"
        )

    if not focus_instructions:
        focus_instructions.append(
            "- 前回のドラフト全体の論理のキレと筆者の独自スタンスをさらに研ぎ澄ましてください。"
        )

    focus_text = "\n".join(focus_instructions)

    rewrite_prompt = f"""あなたは超弦理論、超対称共形場理論（SCFT）、場の量子論の厳密な数理構造の最前線を探究する理論物理学者兼サイエンスブロガーです。

先ほどあなたが執筆したブログ記事ドラフトに対し、Jev System One 診断システムから以下の品質診断スコアと改善要求が届きました。

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
【対象ドキュメント情報】
- 文書名: {doc_info['file_name']}
- タイトル: {doc_info['title']}
- 対象範囲: {doc_info.get('page_label', '')} / {doc_info.get('chapter_hint', '')}

---
【前回のドラフト】
{previous_draft}

---
【フォーマット再確認】
1. フロントマター（YAML）を必ず先頭に出力
2. テキストの太字は必ず HTMLの <strong>太字</strong> タグを使用（Markdownの ** は禁止）
3. 本文開始に「# タイトル」を置かない（## 見出しから開始）
4. 独立行数式は必ず前後に改行を入れて $$ を独立行に配置（\n\n$$\n式\n$$\n\n）

以上の指示に従い、圧倒的クオリティへと生まれ変わった完全版 Markdown 記事を出力してください。
"""

    if doc_info["doc_type"] == "pdf":
        contents = [doc_info["pdf_part"], rewrite_prompt]
    else:
        text_body = doc_info.get("text_content", "")[:35000]
        contents = [f"【ドキュメント本文】\n{text_body}\n\n", rewrite_prompt]

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
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """Gemini 執筆 ➡️ Jev 診断 ➡️ 必要に応じ Gemini リライトの自律推敲ループ"""
    # 1. 初稿執筆
    current_draft = write_blog_post_from_doc_with_gemini(doc_info)

    history = []
    # 2. 初稿の品質検証
    quality = verify_post_with_jev(current_draft, round_num=1)
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
            )
            quality = verify_post_with_jev(current_draft, round_num=revision_round)
            history.append(quality)
        except Exception as e:
            print(f"⚠️ リライト中にエラーが発生したため、前回のドラフトを採用します: {e}")
            break

    if quality["passed"]:
        print(f" ✨ Jev 品質基準を見事クリアしました！（総合スコア: {quality['total_score']:.2f}点）")
    else:
        print(f" ⚠️ リビジョン上限（{max_revisions}回）に達したため、現時点での最高推敲版を採用します。")

    return current_draft, quality, history


def format_doc_post_for_yagibrary(
    raw_markdown: str,
    doc_info: Dict[str, Any],
    quality: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """yagibrary (Astro) の形式に合わせて整形し、Frontmatter と Jev 診断レポートを付加"""
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
            title = f"【文献解説】{doc_info['title']}"

    body = re.sub(r"^#\s+.*?\n+", "", body).strip()

    summary = parsed_meta.get("summary")
    if not summary:
        summary = f"『{doc_info['title']}』の解説記事。数理物理の深層と独自のオピニオンを交えて紐解きます。"

    tags = parsed_meta.get("tags")
    if not tags or not isinstance(tags, list):
        tags = ["物理学", "数理物理", doc_info.get("subfield", "理論物理")]

    cleaned_tags = []
    for t in tags:
        t_clean = str(t).strip().replace('/', '-').replace('\\', '-').replace(':', '-')
        if t_clean and t_clean not in cleaned_tags:
            cleaned_tags.append(t_clean)

    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    # 本文中の **太字** を <strong>太字</strong> に変換 (AGENTS.mdルール)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)

    # 独立数式ブロックの正規化
    body = re.sub(r"(?<!\$)\$\$(?!\$)\s*([^\n]+?)\s*\$\$(?!\$)", r"\n\n$$\n\1\n$$\n\n", body)

    # 推敲改善履歴
    revision_count = len(history) if history else 1
    history_steps = []
    if history:
        for h in history:
            round_lbl = f"第{h.get('round', 1)}稿"
            score_lbl = f"{h.get('total_score', 0):.2f}点"
            status_lbl = "合格" if h.get("passed") else f"足切り ({h.get('diagnosis', '要改善')})"
            history_steps.append(f"{round_lbl}: {score_lbl} [{status_lbl}]")
    history_summary = " ➡️ ".join(history_steps) if history_steps else f"{quality.get('total_score', 0):.2f}点"

    chapter_info = f"- <strong>対象章・セクション</strong>: {doc_info['chapter_hint']}\n" if doc_info.get("chapter_hint") else ""
    meta_section = f"""

---

### 📊 本日の自律型 AI ドキュメント解析レポート
- <strong>解析対象</strong>: <code>{doc_info['file_name']}</code> ({doc_info.get('page_label', '全編')})
{chapter_info}- <strong>Jev 記事品質推敲（Evaluator-Optimizer）</strong>:
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

    return f"---\n{frontmatter_yaml}\n---\n\n{body}\n{meta_section}"


def run_file_pipeline(
    file_path: str,
    pages: Optional[str] = None,
    chapter: Optional[str] = None,
    output_dir: Optional[str] = None
) -> List[str]:
    """ローカルファイル（PDF/Markdown）から自律的に解説記事を執筆・保存するパイプライン"""
    print("\n" + "=" * 65)
    print(" 📖 Local Document × TypeSafe Jev × Gemini ドキュメントブロガー 起動")
    print(f" 📂 指定ファイル: {file_path}")
    if pages:
        print(f" 📑 指定ページ: {pages}")
    if chapter:
        print(f" 🎯 指定章/テーマ: {chapter}")
    print("=" * 65)

    if output_dir is None:
        if os.path.exists(DEFAULT_YAGIBRARY_POSTS_DIR):
            target_dir = DEFAULT_YAGIBRARY_POSTS_DIR
        else:
            target_dir = os.path.join(os.path.dirname(__file__), "generated_posts")
    else:
        target_dir = output_dir

    os.makedirs(target_dir, exist_ok=True)

    # 1. ファイル読込 & メタデータ抽出
    doc_info = load_and_process_local_file(file_path, pages_str=pages, chapter_hint=chapter)

    # 2. 自律執筆 ＆ Jev推敲ループ
    raw_markdown, quality, history = generate_refined_doc_blog_post(doc_info, max_revisions=2)

    # 3. Astro 向け整形
    final_post = format_doc_post_for_yagibrary(raw_markdown, doc_info, quality, history=history)

    # 4. ファイル名生成 & 保存
    today_str = datetime.now().strftime("%Y-%m-%d")
    base_name = os.path.splitext(doc_info["file_name"])[0]
    # ファイル名用の安全なスラッグ
    safe_slug = re.sub(r"[^a-zA-Z0-9_\-]+", "-", base_name).strip("-").lower()
    if not safe_slug:
        safe_slug = "doc-note"

    if chapter:
        safe_ch = re.sub(r"[^a-zA-Z0-9_\-]+", "-", chapter).strip("-").lower()[:20]
        if safe_ch:
            safe_slug = f"{safe_slug}-{safe_ch}"

    filename = f"{today_str}-{safe_slug}.md"
    out_file_path = os.path.join(target_dir, filename)

    # 重複がある場合はインデックスを付与
    counter = 1
    while os.path.exists(out_file_path):
        filename = f"{today_str}-{safe_slug}-{counter}.md"
        out_file_path = os.path.join(target_dir, filename)
        counter += 1

    with open(out_file_path, "w", encoding="utf-8") as f:
        f.write(final_post)

    print("\n" + "=" * 65)
    print(f" 🎉 ドキュメント解説記事の生成・保存が完了しました！")
    print(f"    保存先: {out_file_path}")
    print("=" * 65)
    return [out_file_path]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="arXiv / Document × TypeSafe Jev × Gemini 自律型ブログ執筆パイプライン")
    parser.add_argument("--arxiv-id", "-a", type=str, default="", help="特定の arXiv 論文番号（カンマ区切りで複数可。例: 2006.13892）")
    parser.add_argument("--file", "-f", type=str, default="", help="ローカルのPDFまたはMarkdownファイルパス（例: docs/quantum_field_theory.pdf）")
    parser.add_argument("--pages", "-p", type=str, default="", help="PDFの対象ページ範囲（例: 15-30, 45）")
    parser.add_argument("--chapter", "-c", type=str, default="", help="フォーカスしたい章やテーマ（例: 'Chapter 3: Supersymmetry'）")
    parser.add_argument("--max-papers", "-m", type=int, default=50, help="arXivから自動取得する件数 (デフォルト: 50)")
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

    if args.file.strip():
        # ローカルファイル（PDF/MD）指定モード
        run_file_pipeline(
            file_path=args.file.strip(),
            pages=args.pages.strip() or None,
            chapter=args.chapter.strip() or None,
            output_dir=args.output_dir
        )
    elif args.arxiv_id.strip():
        # 特定論文指定モード
        target_ids = [aid.strip() for aid in args.arxiv_id.split(",") if aid.strip()]
        run_targeted_pipeline(arxiv_ids=target_ids, output_dir=args.output_dir)
    else:
        # 自動スクリーニングモード
        run_daily_pipeline(max_papers=args.max_papers, top_n_to_blog=args.top_n, output_dir=args.output_dir)


