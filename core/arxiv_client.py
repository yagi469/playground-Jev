"""
core/arxiv_client.py
====================
arXiv API との通信、Atom XML レスポンスのパース、HTML/ar5iv による論文本文・主要セクションの抽出
"""

import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup


def _parse_arxiv_xml(xml_text: str) -> List[Dict[str, Any]]:
    """arXiv API の Atom XML レスポンスを共通パース"""
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        title = " ".join(title_el.text.split()) if title_el is not None and title_el.text else "Untitled"

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
    """
    print("\n📡 [arXiv API] hep-th (最重要) & math-ph & quant-ph から最新論文を取得中...")

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
