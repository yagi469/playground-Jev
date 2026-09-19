#!/usr/bin/env python3
"""
daily_paper_blogger.py
======================
arXiv (hep-th / quant-ph) から最新の量子コンピュータ・量子情報・ホログラフィ関連の論文を自動取得し、
TypeSafe (Jev) による高速多面スクリーニングで本日のベスト論文を厳選、
Google Gemini で独自の考察・スタンスを盛り込んだブログ解説記事を自動執筆するパイプライン。
"""

import os
import sys
import time
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any, Optional
import httpx
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv(".env.local")
load_dotenv(".env")

from typesafe_sdk import TypeSafeClient, Choice, Score, Noul

try:
    from google import genai
    gemini_client = genai.Client()
except Exception as e:
    print(f"Gemini Client 初期化警告: {e}")
    gemini_client = None

typesafe_api_key = os.getenv("TYPESAFE_API_KEY")
if not typesafe_api_key:
    raise ValueError("TYPESAFE_API_KEY が設定されていません。.env.local を確認してください。")

typesafe_client = TypeSafeClient(api_key=typesafe_api_key)


# ==============================================================================
# 1. arXiv API から最新論文を取得
# ==============================================================================
def fetch_arxiv_papers(max_results: int = 25) -> List[Dict[str, Any]]:
    """
    hep-th (高エネルギー理論) と quant-ph (量子情報・量子物理) の最新論文を取得
    """
    print(f"\n📡 [arXiv API] hep-th & quant-ph から最新 {max_results} 件の論文を取得中...")
    url = (
        "https://export.arxiv.org/api/query?"
        "search_query=cat:hep-th+OR+cat:quant-ph&"
        "sortBy=submittedDate&sortOrder=descending&"
        f"max_results={max_results}"
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/atom+xml",
    }

    try:
        response = httpx.get(url, headers=headers, timeout=25.0)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ arXiv API 取得失敗: {e}")
        return []

    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []

    for entry in root.findall("atom:entry", ns):
        # タイトル (改行や余分な空白を除去)
        title_el = entry.find("atom:title", ns)
        title = " ".join(title_el.text.split()) if title_el is not None else "Untitled"

        # アブストラクト
        summary_el = entry.find("atom:summary", ns)
        summary = " ".join(summary_el.text.split()) if summary_el is not None else ""

        # arXiv ID と URL
        id_el = entry.find("atom:id", ns)
        raw_id_url = id_el.text.strip() if id_el is not None else ""
        arxiv_id = raw_id_url.split("/abs/")[-1] if "/abs/" in raw_id_url else raw_id_url

        # 公開日
        published_el = entry.find("atom:published", ns)
        published = published_el.text[:10] if published_el is not None else ""

        # 著者
        authors = []
        for author in entry.findall("atom:author", ns):
            name_el = author.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        # カテゴリ
        categories = []
        for cat in entry.findall("atom:category", ns):
            term = cat.attrib.get("term")
            if term:
                categories.append(term)

        # PDF リンク
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

        papers.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "summary": summary,
            "published": published,
            "authors": authors[:5], # 最大5名
            "categories": categories,
            "pdf_url": pdf_url,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
        })

    print(f"✅ {len(papers)} 件の論文メタデータを取得完了")
    return papers


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
        # 1. 量子コンピュータ・量子情報・ホログラフィ領域への合致度
        "is_quantum_relevant": Noul(
            instructions=(
                "この論文は、量子コンピュータ、量子情報、量子誤り訂正、"
                "または高エネルギー理論との融合領域（AdS/CFTホログラフィ、量子エンタングルメント、"
                "量子複雑性、テンソルネットワーク、ブラックホール量子情報など）に明確に関連していますか？"
            )
        ),
        # 2. ユーザー個人の興味・数理物理関心への合致度 (Google Drive文献プロファイル準拠)
        "user_interest_match": Score(
            instructions=(
                f"ユーザーの興味基準に基づき、この論文がユーザーの研究的関心にどれだけマッチするか評価してください。\n"
                f"【ユーザー関心基準】: {user_criteria if user_criteria else '超対称共形場理論、AdS/CFT、量子エンタングルメント、数理物理'}"
            ),
            criteria=[
                "全く関心外（単なる素粒子実験フィッティングや無関係な宇宙論モデルなど）",
                "やや関連（量子情報の一般的応用など）",
                "強くマッチ（ホログラフィ、テンソルネットワーク、量子多体、場の理論の数理的側面）",
                "ドンピシャ（超対称場論、SCFT、4d-2d対応、Dブレーン幾何、AdS/CFTの厳密な量子情報対応）",
            ],
        ),
        # 3. 理論的深さ・新規性
        "theoretical_depth": Score(
            instructions="この論文の理論的深さや数学・物理学的な新規性レベルを評価してください",
            criteria=[
                "初歩的・既存のレビューや軽微な計算",
                "標準的な応用や既存枠組み内の進展",
                "独自の新たな発見・非自明な理論的ブレイクスルーがある",
                "極めて重要な金字塔・パラダイムシフトの可能性を秘める",
            ],
        ),
        # 4. ブログ読者への面白さ・話題性
        "blog_appeal": Score(
            instructions=(
                "物理学や量子技術に関心を持つブログ読者にとって、"
                "直感的に面白く、知的好奇心を刺激する話題性や魅力があるかを評価してください"
            ),
            criteria=[
                "地味・極端な専門家以外には伝わりにくい",
                "普通・学術的価値はあるが一般の興味を惹きにくい",
                "魅力的・直感的な比喩や概念で読者を惹きつけられる",
                "非常に魅力的・『これはすごい』と直感的に共有・議論したくなる",
            ],
        ),
        # 5. サブ分野の特定
        "subfield": Choice(
            instructions="この論文が最も強くフォーカスしているサブ分野を分類してください",
            criteria={
                "quantum_error_correction": "量子誤り訂正符号・フォールトトレラント量子計算 (FTQC)",
                "holography_quantum_gravity": "ホログラフィ・AdS/CFT・ブラックホール量子情報・アイランド公式",
                "scft_mathematical_physics": "超対称共形場理論 (SCFT)・カイラル代数・非摂動QFT・数理物理",
                "quantum_complexity_algorithms": "量子計算複雑性・量子アルゴリズム・量子超越性",
                "tensor_networks_manybody": "テンソルネットワーク・量子多体系・SYK模型・エンタングルメント相転移",
                "other_unrelated": "量子情報や理論物理の数理構造とは無関係な現象論など",
            },
        ),
    }

    start_t = time.time()

    for idx, p in enumerate(papers):
        # 論文テキスト（State）
        state_text = f"Title: {p['title']}\nCategories: {', '.join(p['categories'])}\nAbstract: {p['summary']}"

        try:
            res = typesafe_client.system_one(
                state={"paper": state_text},
                questions=questions,
            )

            is_rel = res.nouls["is_quantum_relevant"].noul
            u_match = res.scores["user_interest_match"].score
            t_depth = res.scores["theoretical_depth"].score
            b_appeal = res.scores["blog_appeal"].score
            s_field = res.choices["subfield"].choice

            # 総合スコア計算
            # 無関係カテゴリはペナルティ
            penalty = 0.2 if s_field == "other_unrelated" else 1.0
            # ユーザー興味マッチ度 (u_match) を強く重み付け (2.5)
            total_score = ((is_rel * 2.5) + (u_match * 2.5) + (t_depth * 1.5) + (b_appeal * 2.0)) * penalty

            p["jev_metrics"] = {
                "is_quantum_relevant": round(is_rel, 3),
                "user_interest_match": round(u_match, 3),
                "theoretical_depth": round(t_depth, 3),
                "blog_appeal": round(b_appeal, 3),
                "subfield": s_field,
                "total_score": round(total_score, 3),
            }
            scored_papers.append(p)

            print(f"  [{idx+1}/{len(papers)}] {p['arxiv_id']} | 関連度: {is_rel:.1%} | 興味合致: {u_match:.2f}/3 | 深度: {t_depth:.1f} | 魅力: {b_appeal:.1f} | 分野: {s_field} | 総合: {total_score:.2f}")

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
def write_blog_post_with_gemini(paper: Dict[str, Any]) -> str:
    """
    選定されたベスト論文をもとに、筆者の熱量と考察が入ったブログ記事を自動執筆
    """
    global gemini_client
    if not gemini_client:
        try:
            from google import genai
            gemini_client = genai.Client()
        except Exception as e:
            raise RuntimeError(f"Gemini Client 初期化エラー: {e}")

    m = paper["jev_metrics"]
    print(f"\n🧠 [Gemini] 総合第1位の論文 {paper['arxiv_id']} のブログ記事を執筆中...")
    print(f"   タイトル: {paper['title']}")
    print(f"   分野: {m['subfield']} (総合スコア: {m['total_score']})")

    user_profile = load_user_interests()
    user_perspective = ""
    if user_profile:
        themes = ", ".join(user_profile.get("core_themes", []))
        user_perspective = f"\n【筆者の専門的バックボーン・着眼点（Google Driveの蔵書・関心より）】\n- 筆者は場の量子論（ワインバーグ流の厳密性）、超対称共形場理論（SCFT）、カイラル代数、Dブレーン幾何、トポロジカル場論（TQFT）、AdS/CFT対応などの数理的側面に強い思い入れがあります。\n- 関心テーマ: {themes}\n- 「で、私（筆者）はどう考えるか？」のセクションでは、これらの数理物理や非摂動的・対称性的観点も交えつつ、独自の一歩踏み込んだ深いオピニオンを熱量高く語ってください。\n"

    prompt = f"""あなたは数理物理学、量子情報理論、超弦理論（AdS/CFTなど）の最前線をわかりやすく発信する、一流の科学ブロガー兼研究者です。
読者が「で、あなたの意見は？」と突っ込みたくなるような退屈なAIまとめ記事ではなく、
あなた自身の独自の切り口・熱量・生々しい所感・直感的なたとえ話を交えた、ワクワクする魅力的なブログ記事を執筆してください。
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
- 量子情報・物理合致度: {m['is_quantum_relevant'] * 100:.1f}%
- ユーザー関心合致スコア: {m.get('user_interest_match', 0.0):.2f} / 3.0
- 理論的深さスコア: {m['theoretical_depth']:.2f} / 3.0
- ブログ話題性スコア: {m['blog_appeal']:.2f} / 3.0
- 専門領域: {m['subfield']}

---
【記事の構成と執筆ルール】
1. **ブログタイトル**:
   - 読者が思わずクリックしたくなる、知的好奇心を刺激するキャッチーな日本語タイトル（「# タイトル」で始める）。
   - 難解な数式名だけでなく、「何が解決したのか」「なぜアツいのか」が直感的に伝わるタイトル。
2. **導入（1行サマリー ＆ つかみ）**:
   - 「この記事でわかること」を明確にし、読者の興味を一気に引き込む導入。
3. **背景にある物理の壁（従来の何が困っていたのか）**:
   - 専門知識がない人でもイメージできるように、身近な日常の比喩や直感的な概念を用いて背景を説明。
4. **この論文の核心アイデア・何をやったのか**:
   - 著者たちがどうやってその壁を乗り越えたのか。数式を丸写しするのではなく、「幾何学的な直感」や「量子回路的な直感」に翻訳して解説。
5. **「で、私（筆者）はどう考えるか？」（★最重要：独自のスタンス・考察）**:
   - 単なる解説で終わらせず、「正直ここが面白い」「一方で、この仮定は現実的なFTQC（またはAdS/CFT）で成り立つのか？」「今後の研究の方向性」など、筆者自身の率直なオピニオン・ツッコミを展開する。
6. **まとめ ＆ 論文リンク**:
   - 記事の総括と、arXivへのリンク、PDFリンク。

Markdown 形式（GitHub Flavored Markdown）で全文を出力してください。
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
# 4. Jev による推敲チェック & スコア付与
# ==============================================================================
def verify_post_with_jev(post_content: str) -> Dict[str, Any]:
    """
    生成されたブログ記事に対して「読者の本音・オピニオン度チェック」を実行
    """
    print(f"\n⚡ [TypeSafe Jev] 生成されたブログ記事のオピニオン度・体温を検証中...")

    questions = {
        "author_stance": Score(
            instructions="記事全体を通して、筆者自身の立場・主張・意思決定・価値観がどれだけ鮮明に打ち出されているかを評価してください",
            criteria=[
                "事実や一般論の要約のみで、筆者の主観や立場が皆無",
                "末尾に形式的な感想がある程度で、スタンスが曖昧",
                "筆者自身の明確な見解や立場が示されており、考えが伝わる",
                "強烈な独自オピニオンや独自の切り口があり、誰が書いたかが一目瞭然",
            ],
        ),
        "lack_of_opinion_risk": Noul(
            instructions="情報や事実の客観的なまとめに終始しており、読者が読み終わった後に「結局、筆者はどう思っているの？」と物足りなさや肩透かしを感じるリスクがありますか？"
        ),
        "reader_impression": Choice(
            instructions="この記事を読んだ読者が直感的に抱く最も強い印象はどれですか？",
            criteria={
                "generic_summary": "「よくあるまとめ記事。ググればすぐわかる」",
                "wants_opinion": "「事実は分かった。で、あなたは賛成なの？」",
                "empathy_insight": "「なるほど！この人の視点や試行錯誤はリアルで面白い」",
                "thought_provoking": "「独自の鋭い切り口で、議論や考察が深まる」",
            },
        ),
    }

    try:
        res = typesafe_client.system_one(state={"post": post_content}, questions=questions)
        stance = res.scores["author_stance"].score
        risk = res.nouls["lack_of_opinion_risk"].noul
        impression = res.choices["reader_impression"].choice

        print(f"  ✓ 独自オピニオン度: {stance:.2f} / 3.0")
        print(f"  ✓ 「あなたの意見は？」リスク: {risk:.1%}")
        print(f"  ✓ 読者の第一印象: {impression}")

        return {
            "stance": stance,
            "lack_of_opinion_risk": risk,
            "impression": impression,
        }
    except Exception as e:
        print(f"⚠️ 検証スキップ: {e}")
        return {}


# ==============================================================================
# メイン実行関数
# ==============================================================================
def run_daily_pipeline(max_papers: int = 15, output_dir: str = "generated_posts") -> str:
    print("\n" + "=" * 65)
    print(" 🚀 arXiv × TypeSafe Jev × Gemini 全自動論文ブロガー 起動")
    print(" 🎯 対象カテゴリ: hep-th (高エネルギー理論) & quant-ph (量子情報)")
    print("=" * 65)

    # 1. 論文取得
    papers = fetch_arxiv_papers(max_results=max_papers)
    if not papers:
        print("❌ 論文を取得できませんでした。終了します。")
        return ""

    # 2. Jev で採点 & ランキング
    ranked_papers = screen_and_rank_papers_with_jev(papers)
    if not ranked_papers:
        print("❌ スクリーニングに失敗しました。")
        return ""

    # 上位3件をサマリー表示
    print("\n🏆 【本日の TOP 3 厳選論文】")
    for i, p in enumerate(ranked_papers[:3]):
        m = p["jev_metrics"]
        print(f"  第{i+1}位: [{p['arxiv_id']}] {p['title'][:65]}...")
        print(f"         総合スコア: {m['total_score']} | 関連度: {m['is_quantum_relevant']:.1%} | 魅力: {m['blog_appeal']:.1f} | {m['subfield']}")

    best_paper = ranked_papers[0]

    # 3. Gemini によるブログ記事執筆
    post_markdown = write_blog_post_with_gemini(best_paper)

    # 4. Jev による推敲オピニオンチェック
    quality = verify_post_with_jev(post_markdown)

    # 記事末尾に Jev の診断メタデータを追加
    meta_section = f"""

---

### 📊 本日の自律型 AI パイプライン採点レポート
- **選定元**: arXiv ({best_paper['arxiv_id']}) / カテゴリ: {', '.join(best_paper['categories'])}
- **Jev スクリーニングスコア**:
  - 量子情報・物理合致度: `{best_paper['jev_metrics']['is_quantum_relevant']*100:.1f}%`
  - 理論的新規性・深度: `{best_paper['jev_metrics']['theoretical_depth']:.2f} / 3.0`
  - 話題性・アピール度: `{best_paper['jev_metrics']['blog_appeal']:.2f} / 3.0`
  - サブ領域: `{best_paper['jev_metrics']['subfield']}`
- **記事のオピニオン診断**:
  - 筆者スタンス度: `{quality.get('stance', 0):.2f} / 3.0`
  - 「で、あなたの意見は？」リスク: `{quality.get('lack_of_opinion_risk', 0)*100:.1f}%`
  - 読者印象: `{quality.get('impression', 'N/A')}`
"""
    final_post = post_markdown + meta_section

    # 5. ファイル保存
    os.makedirs(output_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    clean_id = best_paper['arxiv_id'].replace('/', '_')
    filename = f"{today_str}-arxiv-{clean_id}.md"
    file_path = os.path.join(output_dir, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_post)

    print("\n" + "=" * 65)
    print(f" 🎉 ブログ記事の自動生成が完了しました！")
    print(f" 📝 保存先: {file_path}")
    print("=" * 65)
    return file_path


if __name__ == "__main__":
    count = 10
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
        except ValueError:
            pass
    run_daily_pipeline(max_papers=count)
