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

import base64

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = None
    PdfWriter = None

try:
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
except ImportError:
    fitz = None



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
# 1.5. PDF / arXiv 図表（Figure）抽出 & 埋め込みユーティリティ
# ==============================================================================
def extract_pdf_figures(
    pdf_path: str,
    pages_str: Optional[str] = None,
    page_indices: Optional[List[int]] = None,
    max_figures: int = 8,
    min_width: int = 120,
    min_height: int = 60,
    skip_front_matter: bool = True,
) -> List[Dict[str, Any]]:
    """
    PDFの指定ページから図（画像オブジェクト・グラフ・回路図・ダイアグラム等）を抽出し、
    Base64データURLとメタデータ（プレースホルダー、ページ番号、サイズ等）を生成して返す。
    書籍等のスキャンPDF（全ページが画像）の場合、表紙や目次など本文外のページを自動除外します。
    """
    if fitz is None:
        print("  ⚠️ PyMuPDF (fitz) が利用できないため、PDF図表抽出をスキップします。'pip install pymupdf' を推奨します。")
        return []

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"  ⚠️ PDF図表抽出オープン失敗 ({pdf_path}): {e}")
        return []

    total_pages = len(doc)
    selected_indices = []

    if page_indices is not None and len(page_indices) > 0:
        # 直接インデックスリストが渡された場合
        selected_indices = [idx for idx in page_indices if 0 <= idx < total_pages]
    elif pages_str and pages_str.strip():
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
                    selected_indices.append(idx)
            else:
                idx = int(part) - 1
                if 0 <= idx < total_pages:
                    selected_indices.append(idx)
    else:
        # ページ指定なしの場合
        if total_pages > 20 and skip_front_matter:
            # 書籍等の長大PDFでは、先頭10ページ（表紙、まえがき、目次等）をスキップして本文側を走査
            start_idx = min(10, total_pages - 1)
            selected_indices = list(range(start_idx, min(start_idx + 40, total_pages)))
        else:
            selected_indices = list(range(min(30, total_pages)))

    figures = []
    seen_xrefs = set()
    fig_idx = 1

    for pno in selected_indices:
        page = doc[pno]
        page_rect = page.rect
        img_list = page.get_images()

        # スキャン型PDF判定: ページ内に1枚だけ画像があり、それがページ全体（幅・高さが80%以上）を占めるか
        is_single_full_page = False
        if len(img_list) == 1:
            try:
                base_img_probe = doc.extract_image(img_list[0][0])
                img_w = base_img_probe.get("width", 0)
                img_h = base_img_probe.get("height", 0)
                if page_rect.width > 0 and page_rect.height > 0:
                    w_ratio = img_w / page_rect.width
                    h_ratio = img_h / page_rect.height
                    # スキャンPDFはアスペクト比がほぼページ全体と一致
                    if w_ratio > 0.8 and h_ratio > 0.8:
                        is_single_full_page = True
            except Exception:
                pass

        # 書籍スキャンの先頭（表紙・中扉・目次）は図表として扱わない
        if is_single_full_page and skip_front_matter and pno < 15:
            continue

        for img in img_list:
            if len(figures) >= max_figures:
                break
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                base_img = doc.extract_image(xref)
            except Exception:
                continue

            img_bytes = base_img["image"]
            ext = base_img["ext"]
            w = base_img["width"]
            h = base_img["height"]

            # 極小アイコンや細線・装飾（幅120px未満または高さ60px未満）はスキップ
            if w < min_width or h < min_height:
                continue

            b64_str = base64.b64encode(img_bytes).decode("utf-8")
            mime_type = "image/png" if ext == "png" else f"image/{ext}"
            data_url = f"data:{mime_type};base64,{b64_str}"
            placeholder = f"{{{{PDF_FIGURE_{fig_idx}}}}}"

            figures.append({
                "placeholder": placeholder,
                "data_url": data_url,
                "bytes": img_bytes,
                "mime_type": mime_type,
                "ext": ext,
                "width": w,
                "height": h,
                "page": pno + 1,
                "is_full_page": is_single_full_page,
            })
            fig_idx += 1

        if len(figures) >= max_figures:
            break

    return figures


def fetch_arxiv_paper_figures(paper: Dict[str, Any], max_figures: int = 6) -> List[Dict[str, Any]]:
    """arXiv 論文の PDF を一時ダウンロードし、図表（Figure）を抽出する"""
    pdf_url = paper.get("pdf_url")
    if not pdf_url:
        return []

    print(f"  📥 [arXiv PDF] 論文PDFから図表を抽出中: {pdf_url}...")
    temp_dir = os.path.join(os.path.dirname(__file__), "temp_arxiv_pdfs")
    os.makedirs(temp_dir, exist_ok=True)

    clean_id = paper['arxiv_id'].replace('/', '_').replace('.', '-')
    temp_pdf_path = os.path.join(temp_dir, f"{clean_id}.pdf")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    try:
        # すでにダウンロード済みでなければダウンロード
        if not os.path.exists(temp_pdf_path) or os.path.getsize(temp_pdf_path) == 0:
            resp = httpx.get(pdf_url, headers=headers, follow_redirects=True, timeout=30.0)
            if resp.status_code == 200 and len(resp.content) > 1000:
                with open(temp_pdf_path, "wb") as f:
                    f.write(resp.content)
            else:
                print(f"  ⚠️ arXiv PDF ダウンロード失敗 (ステータス: {resp.status_code})")
                return []

        figures = extract_pdf_figures(temp_pdf_path, max_figures=max_figures)
        if figures:
            print(f"  🖼️ [arXiv 図表抽出] {len(figures)} 点の図表を論文から抽出しました！")
        return figures
    except Exception as e:
        print(f"  ⚠️ arXiv 図表抽出エラー: {e}")
        return []


def build_figures_prompt_components(
    figures: Optional[List[Dict[str, Any]]],
    send_image_parts: bool = False,
) -> Tuple[str, List[Any]]:
    """
    図表リストから Gemini 用のプロンプト指示テキストと Part オブジェクトのリストを生成。
    send_image_parts=False (デフォルト) の場合、Gemini に画像バイナリを送信せずテキストメタデータのみを渡し、
    入力トークン消費を完全にゼロに抑えます。
    """
    if not figures:
        return "", []

    from google.genai import types
    fig_lines = []
    fig_parts = []

    for f in figures:
        fig_lines.append(
            f"- `{f['placeholder']}`: (文献 p.{f['page']} より抽出された図表、サイズ {f['width']}x{f['height']}) "
            f"-> 本文の該当する概念・モデル・実験グラフ・アーキテクチャ図・設計図を解説する直後に、独立した行で `![図の適切なキャプション]({f['placeholder']})` として配置してください。"
        )
        if send_image_parts:
            fig_parts.append(
                types.Part.from_bytes(data=f["bytes"], mime_type=f["mime_type"])
            )

    figures_instruction = f"""
【★文献から抽出された図表（Figure）の選択的配置指示】
文献から以下の {len(figures)} 点の図表候補が抽出されています。
あなたが執筆する解説文の文脈（概念図、アーキテクチャ図、グラフ、実験結果などを説明する箇所）に真に合致する場合にのみ、
ふさわしい位置に以下のプレースホルダーを用いて Markdown 画像構文を挿入してください：
{chr(10).join(fig_lines)}
※重要：文脈に合致しない図表や、書籍の表紙・目次・白紙等の不要な図表は、無理に記事に挿入しないでください（不要な図表は省略して結構です）。
※プレースホルダー記号（`{{{{PDF_FIGURE_1}}}}` など）を出力するだけで結構です（保存時に自動的にBase64画像へと置換されます）。
"""
    return figures_instruction, fig_parts



def embed_figures_in_markdown(
    body: str,
    figures: Optional[List[Dict[str, Any]]],
    auto_fallback: bool = False,
) -> str:
    """プレースホルダー {{PDF_FIGURE_X}} を Base64 データURLに置換。未配置の図は無理に強制挿入しない"""
    if not figures:
        return body

    for f in figures:
        p_holder = f["placeholder"]
        data_url = f["data_url"]
        if p_holder in body:
            body = body.replace(p_holder, data_url)
            print(f"  🖼️ 図の埋め込み成功: {p_holder} (p.{f['page']}, {f['width']}x{f['height']})")
        elif auto_fallback:
            # 明示的に auto_fallback=True の場合のみ末尾フォールバック挿入（arXiv論文モード等）
            fallback_img = f"\n\n![文献 p.{f['page']} より抽出された図表]({data_url})\n\n"
            if "## まとめ" in body:
                body = body.replace("## まとめ", f"{fallback_img}## まとめ", 1)
            elif "## で、私" in body:
                body = body.replace("## で、私", f"{fallback_img}## で、私", 1)
            else:
                body += fallback_img
            print(f"  🖼️ 図の自動配置（フォールバック挿入）: p.{f['page']} ({f['width']}x{f['height']})")

    return body



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
def write_blog_post_with_gemini(
    paper: Dict[str, Any],
    rank: int = 1,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> str:
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

    related_context = ""
    if relevant_posts:
        lines = []
        for rp in relevant_posts:
            lines.append(f"- [{rp['title']}]({rp['url']}) (概要: {rp.get('summary', '')[:100]})")
        related_context = f"\n【当ブログの関連する過去記事（文脈の記憶）】\n当ブログには以下の過去記事が存在します。本文の論理展開の中で、関連する概念や背景・先行理論に触れる際、自然に以下の過去記事への言及・リンク（例: [タイトル](/posts/slug)）を1〜2箇所織り交ぜて、ブログ全体の知識ネットワークを有機的に繋げてください：\n" + "\n".join(lines) + "\n"

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

    # 図表（Figure）プロンプト部品の生成
    figures_instruction, fig_parts = build_figures_prompt_components(paper.get("figures"))

    prompt = f"""あなたは超弦理論、超対称共形場理論（SCFT）、場の量子論の厳密な数理構造（代数・幾何）、AdS/CFT対応の最前線を探究する、一流の理論物理学者兼サイエンスブロガーです。
読者が「で、あなたの意見は？」と突っ込みたくなるような退屈なAIまとめ記事ではなく、
安易で子供騙しな日常のたとえ話（コーヒーの冷却など）に逃げず、理論物理の真の美しさ・対称性の幾何・代数的機構を生き生きと語り尽くす、知的好奇心を刺激する熱いブログ記事を執筆してください。
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
            response = gemini_client.models.generate_content(
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


# ==============================================================================
# 4. Jev による多面品質検証 & Evaluator-Optimizer リライトループ
# ==============================================================================
from score_post import verify_post_with_jev, classify_genre_with_jev



def rewrite_blog_post_with_gemini(
    paper: Dict[str, Any],
    previous_draft: str,
    feedback_metrics: Dict[str, Any],
    revision_round: int,
    rank: int = 1,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
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
4. 数式ブロックは必ず独立した行（$$\n数式\n$$）で出力してください。
5. 前回のドラフトに含まれる図表プレースホルダー（{{PDF_FIGURE_X}}）や画像構文は削除せず、解説の文脈に合わせて維持または適切な位置に配置してください。

知的好奇心と数理的深みに満ちた、決定版となる修正後Markdown記事を出力してください。
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
            response = gemini_client.models.generate_content(
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
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """
    Evaluator-Optimizer パターンによる自律改善パイプライン:
    1. Gemini で初回ドラフトを執筆
    2. Jev で多面品質を厳密採点・足切り判定
    3. 不合格の場合、Jev の指摘を Gemini にフィードバックしてリライト（最大 max_revisions 回）
    4. 最終記事ドラフト、最終品質メトリクス、改善履歴リストを返す
    """
    # Step 1: 初回執筆
    current_post = write_blog_post_with_gemini(paper, rank=rank, relevant_posts=relevant_posts)
    
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
            relevant_posts=relevant_posts,
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


def load_existing_posts_index(posts_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    保存先ディレクトリ内の既存記事から、タイトル・要約・タグ・スラッグのインデックスを構築
    """
    if posts_dir is None:
        posts_dir = DEFAULT_YAGIBRARY_POSTS_DIR
    
    index = []
    if not os.path.exists(posts_dir):
        return index

    for fname in os.listdir(posts_dir):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]  # .md を除去
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
    """
    現在の記事のタイトル、タグ、要約/本文から、最も関連度の高い過去記事を 1〜max_matches 本選出
    """
    if not posts_index:
        return []

    scored_posts = []
    current_tag_set = {str(t).lower() for t in current_tags}

    # 論文ID（例: 2609.19075 / 2609-19075）の抽出
    arxiv_id_match = re.search(r"(\d{4}[\.-]\d{4,5})", current_title + " " + (current_slug or ""))
    current_arxiv_core = arxiv_id_match.group(1).replace(".", "-") if arxiv_id_match else None

    # ストップワードの拡充（一般的な助詞、英語冠詞、年号・日付・汎用単語など）
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

        # 1. 同一論文（リメイク版や学部生向け版など）は最高優先度
        if current_arxiv_core and current_arxiv_core in post_slug.replace(".", "-"):
            score += 50

        # 2. タグの一致（強いシグナル）
        common_tags = current_tag_set & post_tags
        score += len(common_tags) * 10

        # 3. 共通キーワード
        post_keywords = {
            w for w in re.findall(r"[\w]+", (post_title + " " + post_summary).lower())
            if len(w) >= 2 and w not in stopwords
        }
        common_words = current_keywords & post_keywords
        score += len(common_words) * 2

        # 4. タグが全く一致せず、キーワード一致も少ない無関係な記事は除外
        if len(common_tags) == 0 and len(common_words) < 3:
            continue

        # 5. ジャンル乖離の防止: 物理・数理記事にビジネス書評やクラウドインフラが混入するのを防ぐ
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
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    figures: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    yagibrary (Astro content collections) のフォーマット仕様に合わせて整形：
    1. title, date, summary, tags の Frontmatter 生成・正規化
    2. 本文冒頭の不要な # 見出しの除去
    3. Markdownの **太字** を HTMLの <strong>太字</strong> に変換（AGENTS.mdルール遵守）
    4. 論文PDFから抽出された図表（Figure）の自動埋め込み
    5. 関連記事リンクセクションを本文末尾に付加
    6. 採点レポートを記事末尾に付加（Evaluator-Optimizer 推敲履歴を含む）
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

    # PDFから抽出された図表プレースホルダー（{{PDF_FIGURE_X}}）を Base64 データURLに置換
    figs_to_embed = figures or paper.get("figures")
    body = embed_figures_in_markdown(body, figs_to_embed)


    # 関連記事セクション（文脈の記憶・ネットワーク）
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

    final_post = f"---\n{frontmatter_yaml}\n---\n\n{body}{related_section}\n{meta_section}"
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

    # 既存記事のインデックス構築 & arXiv ID 検出
    posts_index = load_existing_posts_index(target_dir)
    existing_arxiv_ids = get_existing_arxiv_ids(target_dir)
    if existing_arxiv_ids:
        print(f"📚 既存記事ディレクトリ ({target_dir}) から {len(posts_index)} 件の記事インデックスと執筆済み arXiv ID を照合中...")

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

        # 過去記事インデックスから関連する記事を自動検索
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

        # 2.5. 論文本文（HTML/ar5iv）の重要セクション抽出 & PDF図表抽出
        paper["full_text_content"] = fetch_arxiv_paper_content(paper["arxiv_id"])
        paper["figures"] = fetch_arxiv_paper_figures(paper)

        # 3. Gemini × Jev 自律推敲・リライトループ（Evaluator-Optimizer パターン）
        raw_markdown, quality, history = generate_refined_blog_post(
            paper=paper,
            rank=rank,
            max_revisions=2,
            relevant_posts=relevant_posts,
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
            relevant_posts=relevant_posts,
            figures=paper.get("figures"),
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

    posts_index = load_existing_posts_index(target_dir)

    os.makedirs(target_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    generated_files = []

    for i, paper in enumerate(ranked_papers):
        batch_idx = i + 1
        print("\n" + "-" * 65)
        print(f" 🖋️ [記事執筆・整形 {batch_idx}/{len(ranked_papers)}] 対象論文: {paper['arxiv_id']}")
        print(f"    タイトル: {paper['title']}")
        print("-" * 65)

        # 過去記事インデックスから関連する記事を自動検索
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

        # 論文本文（HTML/ar5iv）の重要セクション抽出 & PDF図表抽出
        paper["full_text_content"] = fetch_arxiv_paper_content(paper["arxiv_id"])
        paper["figures"] = fetch_arxiv_paper_figures(paper)

        # Gemini × Jev 自律推敲・リライトループ
        raw_markdown, quality, history = generate_refined_blog_post(
            paper=paper,
            rank=batch_idx,
            max_revisions=2,
            relevant_posts=relevant_posts,
        )

        time_offset = (len(ranked_papers) - batch_idx) * 60
        final_post = format_post_for_yagibrary(
            raw_markdown,
            paper,
            quality,
            rank=batch_idx,
            time_offset_seconds=time_offset,
            history=history,
            relevant_posts=relevant_posts,
            figures=paper.get("figures"),
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
    """ローカルファイルパスを解決する（OS間の区切り文字の違い、カレント、yagibrary、docs配下の再帰的探索）"""
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

    # 4. docs フォルダ配下のサブディレクトリを再帰的に探索
    target_basename = os.path.basename(normalized)
    possible_names = [target_basename]
    # 拡張子がない場合は .pdf, .md, .txt 等も候補に追加
    if "." not in target_basename:
        possible_names.extend([f"{target_basename}.pdf", f"{target_basename}.md", f"{target_basename}.txt"])

    search_roots = [
        os.path.abspath(os.path.join(script_dir, "../yagibrary/docs")),
        os.path.join(script_dir, "yagibrary/docs"),
        os.path.join(script_dir, "docs"),
    ]

    for root_dir in search_roots:
        if os.path.exists(root_dir) and os.path.isdir(root_dir):
            for root, _, files in os.walk(root_dir):
                for f in files:
                    for name in possible_names:
                        if f.lower() == name.lower():
                            found_path = os.path.normpath(os.path.join(root, f))
                            if os.path.isfile(found_path):
                                return found_path

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


def resolve_chapter_pages_from_toc(
    pdf_path: str,
    chapter_hint: str,
    total_pages: int,
) -> Optional[str]:
    """
    大部数PDF（書籍・マニュアル等）において、目次（TOC）または目次スキャンページから、
    指定された章・テーマのページ範囲（例: '366-378'）を自動特定する。
    """
    if not chapter_hint or total_pages <= 40:
        return None

    # 1. まず PDF 内部の電子しおり（TOC）を走査
    if fitz is not None:
        try:
            doc = fitz.open(pdf_path)
            toc = doc.get_toc()
            if toc:
                hint_lower = chapter_hint.lower().strip()
                for idx, item in enumerate(toc):
                    t = str(item[1]).lower()
                    if hint_lower in t or t in hint_lower:
                        start_p = int(item[2])
                        end_p = start_p + 25
                        if idx + 1 < len(toc):
                            next_p = int(toc[idx + 1][2])
                            if next_p > start_p:
                                end_p = min(next_p + 2, total_pages)
                        end_p = min(end_p, total_pages)
                        doc.close()
                        print(f"   🎯 [PDF目次解析] しおりからページ範囲を特定: p.{start_p}-{end_p} (見出し: {item[1]})")
                        return f"{start_p}-{end_p}"
            doc.close()
        except Exception as e:
            print(f"   ⚠️ PDFしおり走査エラー: {e}")

    # 2. しおりが無い場合（KindleスクショやスキャンPDF）、先頭の目次ページ（p.4〜p.22）を Gemini に解析させてページ番号を特定
    try:
        from google.genai import types
        reader = PdfReader(pdf_path)
        toc_writer = PdfWriter()
        # 目次が存在する可能性の高い範囲（4〜22ページ、または最大25ページ）
        start_toc = min(3, total_pages - 1)
        end_toc = min(22, total_pages)
        for i in range(start_toc, end_toc):
            toc_writer.add_page(reader.pages[i])

        buf = io.BytesIO()
        toc_writer.write(buf)
        toc_bytes = buf.getvalue()

        if len(toc_bytes) > 0:
            toc_part = types.Part.from_bytes(data=toc_bytes, mime_type="application/pdf")
            toc_prompt = f"""添付のPDFは書籍の目次（Table of Contents）抜粋です（全 {total_pages} ページ中の p.{start_toc + 1}-{end_toc}）。
ユーザーが解説を希望しているテーマ/章: 『{chapter_hint}』

目次を精査し、このテーマ/章が扱われている書籍内のページ番号（開始ページと終了ページ、またはその節の開始ページ）を特定してください。
以下のJSON形式のみを出力してください。Markdownの```json ... ```で囲んでください。
{{
  "found": true,
  "chapter_title": "目次に書かれている正確な見出し名",
  "start_page": 366,
  "end_page": 378,
  "page_range_str": "366-378"
}}
※目次から該当する章・節・キーワードが見つからない場合は、{{"found": false}} と出力してください。
"""
            global gemini_client
            if gemini_client is None:
                init_gemini_client()

            print(f"   🔍 [目次AIスキャン] 目次ページ (p.{start_toc + 1}-{end_toc}) を解析して 『{chapter_hint}』 の掲載ページを探索中...")
            res = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[toc_part, toc_prompt]
            )
            raw_text = res.text
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if json_match:
                toc_data = json.loads(json_match.group(1))
            else:
                toc_data = json.loads(raw_text.strip())

            if toc_data.get("found"):
                start_p = int(toc_data.get("start_page", 1))
                end_p = int(toc_data.get("end_page", start_p + 15))
                # ページ番号のバリデーション
                start_p = max(1, min(start_p, total_pages))
                end_p = max(start_p, min(end_p, total_pages))
                # あまりに長すぎる場合は最大35ページ程度に収める
                if end_p - start_p > 35:
                    end_p = start_p + 35
                range_str = f"{start_p}-{end_p}"
                print(f"   🎯 [目次AIスキャン成功] 『{chapter_hint}』の掲載ページを自動特定しました: p.{range_str} (見出し: {toc_data.get('chapter_title')})")
                return range_str
            else:
                print(f"   ℹ️ 目次内に直接該当する見出しは見つかりませんでした（デフォルト走査を行います）。")
    except Exception as e:
        print(f"   ⚠️ 目次AIスキャン失敗: {e}")

    return None


def load_and_process_local_file(
    file_path: str,
    pages_str: Optional[str] = None,
    chapter_hint: Optional[str] = None,
    genre: Optional[str] = "auto",
) -> Dict[str, Any]:
    """
    ローカルの PDF または Markdown / Text ファイルを読み込み、Gemini 用のコンテンツオブジェクトと
    基本メタデータ（タイトル・要約・ジャンル・サブ領域など）を構造化して返す。
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
        "genre": genre or "auto",
    }

    if ext == ".pdf":
        from google.genai import types
        from pypdf import PdfReader
        total_pages = len(PdfReader(resolved_path).pages)

        # ページ指定がなく章・テーマが指定されている場合、目次から自動特定を試みる
        effective_pages_str = pages_str
        if not effective_pages_str and chapter_hint and total_pages > 40:
            auto_pages = resolve_chapter_pages_from_toc(resolved_path, chapter_hint, total_pages)
            if auto_pages:
                effective_pages_str = auto_pages

        pdf_bytes, page_label = extract_pdf_pages_bytes(resolved_path, effective_pages_str)
        doc_info["page_label"] = page_label
        doc_info["pdf_part"] = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        doc_info["doc_type"] = "pdf"
        print(f"   📑 PDF 抽出完了: {page_label} ({len(pdf_bytes):,} bytes)")

        # 1. Gemini に基本メタデータと対象章のページ範囲特定を依頼
        meta_prompt = f"""添付のPDFドキュメント（抽出範囲: {page_label}、指定テーマ/章: {chapter_hint or '指定なし'}）の内容を読み取り、
以下のJSON形式でメタデータを出力してください。Markdownの```json ... ```形式で囲んでください。
{{
  "title": "このドキュメントまたは対象セクションの的確なタイトル（日本語または英語の原題）",
  "authors": ["著者名または編者名（判明する場合）"],
  "summary": "このドキュメント/対象セクションで論じられている核心内容の要約（150〜250文字）",
  "genre": "business（ビジネス・マネジメント・組織論・経済）, tech（ソフトウェア・システム設計・工学）, physics（数理物理・理論物理・量子・科学論文）, general（一般教養・その他）のいずれか1つを必ず選択",
  "categories": ["ドキュメント内容に即した適切なカテゴリタグ3〜4個（例: マネジメント, 組織論, 生産性 / 場の量子論, 超弦理論 / アーキテクチャ, クラウド 等）"],
  "subfield": "具体的な専門分野やテーマ（例: 組織マネジメント, 生産管理, カイラル代数, 分散システム 等）",
  "chapter_pages": "指定された章やテーマ（{chapter_hint or '指定なし'}）が論じられているPDF内のページ範囲（例: '27-60'、不明な場合は空文字）"
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
                "genre": "general",
                "categories": ["ドキュメント解説", "読書ノート"],
                "subfield": "文献解説",
            }

        doc_info.update(meta)

        # 2. 図表（Figure）の抽出（対象章のページ範囲が判明した場合は絞り込んで抽出）
        effective_pages_str = pages_str
        if not effective_pages_str and meta.get("chapter_pages"):
            ch_pages = str(meta["chapter_pages"]).strip()
            if re.match(r"^\d+\s*-\s*\d+$", ch_pages) or ch_pages.isdigit():
                effective_pages_str = ch_pages
                print(f"   🎯 対象章の特定ページ範囲から図表を抽出します: p.{effective_pages_str}")

        pdf_figures = extract_pdf_figures(
            resolved_path,
            pages_str=effective_pages_str,
            max_figures=8,
            skip_front_matter=True,
        )
        doc_info["figures"] = pdf_figures
        if pdf_figures:
            print(f"   🖼️ PDFから図表（Figure）を {len(pdf_figures)} 点抽出完了")

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
  "genre": "business（ビジネス・マネジメント・組織論・経済）, tech（ソフトウェア・システム設計・工学）, physics（数理物理・理論物理・量子・科学論文）, general（一般教養・その他）のいずれか1つを必ず選択",
  "categories": ["文書内容に即した適切なカテゴリタグ3〜4個"],
  "subfield": "具体的な専門分野やテーマ"
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
                "genre": "general",
                "categories": ["ドキュメント解説", "読書ノート"],
                "subfield": "文献解説",
            }

        doc_info.update(meta)
    else:
        raise ValueError(f"未対応のファイル形式です: {ext} (対応: .pdf, .md, .markdown, .txt)")

    # ジャンル判定: ユーザー指定がない場合は TypeSafe Jev で型安全に自動分類
    if not genre or genre == "auto":
        summary_sample = doc_info.get("summary", "") or doc_info.get("text_content", "")[:1500]
        detected_genre = classify_genre_with_jev(
            title=doc_info.get("title", file_name),
            summary_or_text=summary_sample,
        )
        doc_info["genre"] = detected_genre
    else:
        doc_info["genre"] = genre

    print(f"   🏷️ 認識タイトル: {doc_info.get('title')}")
    print(f"   📚 確定ジャンル: {doc_info.get('genre')}")
    print(f"   🏷️ 専門サブ領域: {doc_info.get('subfield')}")
    return doc_info




def get_genre_blog_config(genre: str, doc_info: Dict[str, Any]) -> Dict[str, Any]:
    """ドキュメントのジャンルに応じたペルソナ、見出し構成、フォーカス指示を返す"""
    title = doc_info.get("title", "")
    ch_hint = doc_info.get("chapter_hint", "")
    ch_str = f" - {ch_hint}" if ch_hint else ""
    subfield = str(doc_info.get("subfield", "")).lower()
    title_lower = title.lower()

    if genre in ["business", "management"]:
        persona = (
            "あなたは経営・組織マネジメント・生産工学・テックリードの最前線を探究する、気鋭のビジネス・テックエッセイストです。\n"
            "ありふれた自己啓発の精神論や浅い常識の焼き直しに逃げず、本書が提示する組織力学の真のロジック、"
            "ボトルネックと因果関係、実践フレームワークを生き生きと語り尽くす、知的好奇心と実務への洞察に満ちたブログ記事を執筆してください。"
        )
        sections = f"""
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象ドキュメント（『{title}』{ch_str}）について言及し、本書が提示する最も本質的な洞察と問題意識を1分で掴むロードマップを示してください。
     - ## 現場を縛る見えない壁と誤謬: なぜ従来のやり方や「良かれと思った常識」が破綻するのか、組織やチームが直面するボトルネックや構造的問題をクリアに解説。
     - ## 核心フレームワークと実践モデル: 著者が提示する中核の思考法、生産・組織モデル、原理原則（例: 朝食工場モデル、制限ステップ、マネジメントレバレッジ、タスク習熟度など）を、具体例を交えて体系的に解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：独自のスタンス・考察・批評）単なる要約で終わらせず、現代のIT・スタートアップ組織や個人の働き方に照らし合わせ、「何が本質で、どこに現代特有の適用限界や落とし穴があるのか」など、骨太なオピニオンを展開。
     - ## まとめ ＆ アクション指針: 記事の総括と、明日からの意思決定・組織運営に活かせる実践的教訓の箇条書き。
"""
        guidance = (
            "- 【最重要：無理なアナロジーの禁止】数理物理の数式や物理学の比喩（量子力学、場の理論など）、分散システムの専門用語などを無理にこじつけて持ち出さないでください。\n"
            "- 生産プロセス、組織ダイナミクス、意思決定の因果関係を、具体的かつ論理的に解き明かしてください。"
        )
        default_tags = ["ビジネス", "マネジメント", "組織論", "生産性"]

    elif genre in ["tech", "engineering"]:
        # ドキュメントが文章術・キャリア・開発文化・プラクティスか、システムアーキテクチャ/低レイヤかを自動判別
        is_writing_or_culture = any(
            k in subfield or k in title_lower
            for k in ["writing", "blog", "文章", "ライティング", "キャリア", "career", "文化", "culture", "コミュニケーション", "communication", "習慣", "チーム"]
        )

        if is_writing_or_culture:
            persona = (
                "あなたはエンジニアリング文化、テクニカルライティング、開発者の生産性向上に情熱を注ぐ、気鋭のテックエッセイスト兼シニアエンジニアです。\n"
                "ありふれた精神論や無菌室の美辞麗句に逃げず、エンジニアが直面する生々しい現実、"
                "文章化がもたらすプログラミング能力の向上やチーム・キャリアへのレバレッジを、親しみやすくかつ知的好奇心に満ちた熱量で語り尽くすブログ記事を執筆してください。"
            )
            sections = f"""
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象ドキュメント（『{title}』{ch_str}）に言及し、本書が提示する最も本質的な洞察と問題意識を提示。
     - ## なぜ多くのエンジニアは書くことを恐れるのか（現場の誤謬と課題）: 「コードだけ書けばいい」という思い込みや、書かないことのよくある言い訳・障壁をリアルに解き明かす。
     - ## 著者が提示する核心アイデア・実践アプローチ: 本書が提示する中核の思考法、言語化のプロセス、コード理解やチーム開発に与える具体的なメリットを体系的に解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：現場に引きつけたオピニオン）単なる要約で終わらせず、現代の開発現場や個人のキャリアに照らし合わせ、「なぜこれが今すべての開発者に必要なのか」を熱く語る。
     - ## まとめ ＆ アクション指針: 記事の総括と、明日から試せる小さな実践ステップ。
"""
            guidance = (
                "- 【最重要：無理なアナロジー・こじつけの禁止】分散システム（Shared-Nothing、メッセージパッシング等）や数理モデル（待ち行列理論、確率過程等）の難解な専門用語を無理にこじつけて持ち出さないでください。\n"
                "- 本書が語る文章術・思考法・チームコミュニケーションのリアルな価値に誠実にフォーカスし、読者が『自分も書いてみよう』と心から思える実践的で親切な記事にしてください。"
            )
            default_tags = ["エンジニアリング", "テクニカルライティング", "キャリア", "生産性"]
        else:
            persona = (
                "あなたは最新のソフトウェア設計、クラウドアーキテクチャ、分散システムの実務に精通したシニアシステムアーキテクト兼テックブロガーです。\n"
                "表層的なチュートリアルやお茶濁しではなく、設計思想の核心、トレードオフ、現場での実践価値を鮮明に語る記事を執筆してください。"
            )
            sections = f"""
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象ドキュメント（『{title}』{ch_str}）に言及し、技術的コアと解決する課題の直観的イメージを提示。
     - ## アーキテクチャの課題と設計の壁: 従来の方式の何が課題だったのか、なぜこの技術・設計が本質的なブレイクスルーなのかを解説。
     - ## 核心メカニズムと実装パターン: システム内部の動作メカニズム、データ構造、通信プロトコル、設計パターンを具体的に解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：技術選定とオピニオン）単なる仕様まとめではなく、現場への導入コスト、運用負荷、トレードオフ、代替技術との比較を深く論じる。
     - ## まとめ ＆ 実装・検証指針: 記事の総括と、現場で試すための実践的ポイント。
"""
            guidance = (
                "- 具体的なアーキテクチャの構成、設計上のトレードオフ、動作原理のロジックを明快に解説してください。\n"
                "- 【注意】ドキュメントの主題から逸脱した、無関係な数式や別分野のモデルを無理にこじつけないでください。"
            )
            default_tags = ["エンジニアリング", "アーキテクチャ", "テクノロジー"]

    elif genre in ["physics", "math"]:
        persona = (
            "あなたは超弦理論、超対称共形場理論（SCFT）、場の量子論の厳密な数理構造（代数・幾何）、AdS/CFT対応の最前線を探究する、一流の理論物理学者兼サイエンスブロガーです。\n"
            "安易で子供騙しな日常のたとえ話（コーヒーの冷却など）に逃げず、理論物理・数理構造の真の美しさ・対称性の幾何・代数的機構を生き生きと語り尽くす、知的好奇心を刺激する熱いブログ記事を執筆してください。"
        )
        sections = f"""
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象ドキュメント（『{title}』{ch_str}）に言及し、難解な数式に入る前に『この記事の核心アイデア（1分で掴む直観的イメージ）』を提示して読者が迷子にならないロードマップを示してください。
     - ## 背景にある物理・数学の壁: 従来の枠組みの何が未解決だったのか、なぜこの理論・概念が本質的なのかを論理的かつクリアに解説。
     - ## 核心アイデアと数理的機構: 著者がどのようなアイデア・数理構造（対称性、代数、幾何学的配位、双対性など）を展開しているかを解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：独自のスタンス・考察・ツッコミ）単なる要約で終わらせず、数理物理・非摂動QFT・超対称性の視点から「ここが美しい」「この仮定・手法はどこまで拡張可能か？」など、研究者としての骨太なオピニオンを展開。
     - ## まとめ ＆ 参考文献・関連情報: 記事の総括と文献情報。
"""
        guidance = "前提知識の導入と途中計算・行間の明示（最重要）。数式ブロックは独立行に出力。"
        default_tags = ["物理学", "数理物理", "理論物理"]

    else:
        persona = (
            "あなたはその分野の背景や思想に精通し、物事の本質を鮮やかに射抜く鋭い批評眼を持つエッセイスト兼ブロガーです。\n"
            "表層的な要約にとどまらず、著者の思想的コア、背景にあるパラダイムシフト、現代への意義を深く掘り下げる記事を執筆してください。"
        )
        sections = f"""
     - ## 導入（1行サマリー ＆ つかみ）: この記事でわかることと読者の知的好奇心を一気に引き込む導入。冒頭で対象ドキュメント（『{title}』{ch_str}）に言及し、核心メッセージを提示。
     - ## 提起された問いとパラダイムの限界: なぜこの問いが生まれたのか、従来の常識や前提が直面していた壁を解説。
     - ## 核心となる洞察と論理展開: 著者が提示する中核のロジック、証拠、思考モデルを具体的に解説。
     - ## で、私（筆者）はどう考えるか？: （★最重要：独自のオピニオン）単なる紹介で終わらせず、現代の課題に引きつけた独自の視点と深い考察を展開。
     - ## まとめ ＆ 思考を深めるヒント: 総括と読者への問いかけ。
"""
        guidance = (
            "- 抽象論に逃げず、具体的な論理の因果関係と著者の核心メッセージを分かりやすく解き明かしてください。\n"
            "- 【注意】無理なアナロジーや無関係な専門用語をこじつけず、ドキュメント本来の知見にフォーカスしてください。"
        )
        default_tags = ["読書論考", "文献解説", "教養"]

    return {
        "persona": persona,
        "sections": sections,
        "guidance": guidance,
        "default_tags": default_tags,
    }


def write_blog_post_from_doc_with_gemini(
    doc_info: Dict[str, Any],
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    PDF または Markdown の内容から、Gemini でジャンル適応型の本格解説ブログ記事（初稿）を執筆
    """
    global gemini_client
    if gemini_client is None:
        init_gemini_client()

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
        related_context = f"\n【当ブログの関連する過去記事（文脈の記憶）】\n当ブログには以下の過去記事が存在します。本文の論理展開の中で、関連する概念や先行理論に触れる際、自然に以下の過去記事への言及・内部リンク（例: [タイトル](/posts/slug)）を1〜2箇所織り交ぜて、ブログ全体の知識ネットワークを有機的に繋げてください：\n" + "\n".join(lines) + "\n"

    tags_sample = "\n".join([f"  - {t}" for t in g_config["default_tags"]])

    # 図表（Figure）プロンプト部品の生成
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
   - ブログ記事内でテキストを太字・強調する場合は、Markdownの ** 記法ではなく、必ず HTMLの <strong> タグ（例: <strong>太字テキスト</strong>）を使用してください。

3. **本文の見出し構成**:
   - 本文の開始部分に「# タイトル」を置かないでください（フロントマターのtitleがWebサイト側で自動描画されるため）。
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
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Jev のフィードバックに基づきドキュメント解説記事を再推敲"""
    global gemini_client
    if gemini_client is None:
        init_gemini_client()

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
                    "※分散システムや数理モデルなどの無関係な専門用語を無理にこじつけないでください。"
                )
            else:
                focus_instructions.append(
                    "- 【最重要：アーキテクチャ・内部メカニズムの具体化】具体的なデータ構造、通信、設計パターンを具体的に掘り下げてください。"
                )
        if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
            focus_instructions.append(
                "- 【最重要：エンジニアリングオピニオンの強化】現場目線の実践的オピニオンやスタンスを鮮明に打ち出してください。"
            )
    else:
        # physics / general
        if feedback_metrics.get("clarity", 0.0) < 2.0 or feedback_metrics.get("rushed_math_risk", 0.0) >= 0.35 or diagnosis == "need_pedagogical_steps":
            focus_instructions.append(
                "- 【最重要：前提知識と途中計算（行間）の徹底解説】読者が置いてけぼりになっています！"
                "  1. 記号や前提となる初等概念との違いを必ず平易に定義・説明してください。\n"
                "  2. 重要な数式については、なぜその式になるのかという『途中計算のステップ』を本文中に明記してください。"
            )
        if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
            focus_instructions.append(
                "- 【最重要：数理的・論理的機構の具体化】具体的な機構がどう論理的に機能しているのかを明快に解説してください。"
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
5. 前回のドラフトに含まれる図表プレースホルダー（{{PDF_FIGURE_X}}）や画像構文は削除せず、解説の文脈に合わせて維持または適切な位置に配置してください。

以上の指示に従い、圧倒的クオリティへと生まれ変わった完全版 Markdown 記事を出力してください。
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
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """Gemini 執筆 ➡️ Jev 診断 ➡️ 必要に応じ Gemini リライトの自律推敲ループ"""
    genre = doc_info.get("genre", "general")
    domain_for_jev = (
        "business" if genre in ["business", "management"]
        else ("tech" if genre in ["tech", "engineering"]
        else ("physics" if genre in ["physics", "math"]
        else "general"))
    )

    # 1. 初稿執筆
    current_draft = write_blog_post_from_doc_with_gemini(doc_info, relevant_posts=relevant_posts)

    history = []
    # 2. 初稿の品質検証
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



def format_doc_post_for_yagibrary(
    raw_markdown: str,
    doc_info: Dict[str, Any],
    quality: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
    relevant_posts: Optional[List[Dict[str, Any]]] = None,
    figures: Optional[List[Dict[str, Any]]] = None,
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

    genre = doc_info.get("genre", "general")
    summary = parsed_meta.get("summary")
    if not summary:
        if genre in ["business", "management"]:
            summary = f"『{doc_info['title']}』の解説記事。組織マネジメントの本質と実践フレームワークを独自のオピニオンを交えて紐解きます。"
        elif genre in ["tech", "engineering"]:
            summary = f"『{doc_info['title']}』の技術解説。アーキテクチャの核心とトレードオフを独自の視点で紐解きます。"
        elif genre in ["physics", "math"]:
            summary = f"『{doc_info['title']}』の解説記事。数理物理の深層と独自のオピニオンを交えて紐解きます。"
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
        else:
            tags = ["読書論考", "文献解説", "教養"]

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

    # PDFから抽出された図表プレースホルダー（{{PDF_FIGURE_X}}）を Base64 データURLに置換
    figs_to_embed = figures or doc_info.get("figures")
    body = embed_figures_in_markdown(body, figs_to_embed)


    # 関連記事セクション（文脈の記憶・ネットワーク）
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

    # 推敲改善履歴
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
    depth_label = "数理・理論の具体性" if genre in ["physics", "math"] else ("技術・アーキテクチャの具体性" if genre in ["tech", "engineering"] else "モデル・因果関係の具体性")
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


def run_file_pipeline(
    file_path: str,
    pages: Optional[str] = None,
    chapter: Optional[str] = None,
    genre: Optional[str] = "auto",
    output_dir: Optional[str] = None,
) -> List[str]:
    """ローカルファイル（PDF/Markdown）から自律的に解説記事を執筆・保存するパイプライン"""
    print("\n" + "=" * 65)
    print(" 📖 Local Document × TypeSafe Jev × Gemini ドキュメントブロガー 起動")
    print(f" 📂 指定ファイル: {file_path}")
    if pages:
        print(f" 📑 指定ページ: {pages}")
    if chapter:
        print(f" 🎯 指定章/テーマ: {chapter}")
    if genre and genre != "auto":
        print(f" 📚 指定ジャンル: {genre}")
    print("=" * 65)

    if output_dir is None:
        if os.path.exists(DEFAULT_YAGIBRARY_POSTS_DIR):
            target_dir = DEFAULT_YAGIBRARY_POSTS_DIR
        else:
            target_dir = os.path.join(os.path.dirname(__file__), "generated_posts")
    else:
        target_dir = output_dir

    os.makedirs(target_dir, exist_ok=True)

    # 1. ファイル読込 & メタデータ抽出（Jev によるジャンル自動分類を含む）
    doc_info = load_and_process_local_file(file_path, pages_str=pages, chapter_hint=chapter, genre=genre)

    # 過去記事インデックスから関連する記事を自動検索
    posts_index = load_existing_posts_index(target_dir)
    doc_tags = doc_info.get("categories", []) + [doc_info.get("subfield", "")]
    relevant_posts = find_relevant_past_posts(
        current_title=doc_info.get("title", ""),
        current_tags=doc_tags,
        current_text=doc_info.get("summary", ""),
        posts_index=posts_index,
        max_matches=3,
    )
    if relevant_posts:
        print(f"  🔗 関連する過去記事を {len(relevant_posts)} 件検出: {[p['title'][:30] for p in relevant_posts]}")

    # 2. 自律執筆 ＆ Jev推敲ループ
    raw_markdown, quality, history = generate_refined_doc_blog_post(
        doc_info,
        max_revisions=2,
        relevant_posts=relevant_posts,
    )

    # 3. Astro 向け整形
    final_post = format_doc_post_for_yagibrary(
        raw_markdown,
        doc_info,
        quality,
        history=history,
        relevant_posts=relevant_posts,
        figures=doc_info.get("figures"),
    )


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
    parser.add_argument("--file", "-f", type=str, default="", help="ローカルのPDFまたはMarkdownファイルパス（例: docs/high_output_management.pdf）")
    parser.add_argument("--pages", "-p", type=str, default="", help="PDFの対象ページ範囲（例: 15-30, 45）")
    parser.add_argument("--chapter", "-c", type=str, default="", help="フォーカスしたい章やテーマ（例: 'Chapter 1: The Basics of Production'）")
    parser.add_argument("--genre", "-g", type=str, default="auto", choices=["auto", "business", "tech", "physics", "general"], help="執筆ジャンル (デフォルト: auto)")
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
            genre=args.genre,
            output_dir=args.output_dir
        )
    elif args.arxiv_id.strip():
        # 特定論文指定モード
        target_ids = [aid.strip() for aid in args.arxiv_id.split(",") if aid.strip()]
        run_targeted_pipeline(arxiv_ids=target_ids, output_dir=args.output_dir)
    else:
        # 自動スクリーニングモード
        run_daily_pipeline(max_papers=args.max_papers, top_n_to_blog=args.top_n, output_dir=args.output_dir)



