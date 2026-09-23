"""
core/figure_extractor.py
========================
PDF / arXiv 図表（Figure）の抽出、Base64エンコード、プロンプト用指示の構築、Markdown画像埋め込み
"""

import os
import re
import base64
from typing import List, Dict, Any, Optional, Tuple
import httpx

try:
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
except ImportError:
    fitz = None


def extract_pdf_figures(
    pdf_path: str,
    pages_str: Optional[str] = None,
    page_indices: Optional[List[int]] = None,
    max_figures: int = 12,
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
        if total_pages > 20 and skip_front_matter:
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

        is_single_full_page = False
        if len(img_list) == 1:
            try:
                base_img_probe = doc.extract_image(img_list[0][0])
                img_w = base_img_probe.get("width", 0)
                img_h = base_img_probe.get("height", 0)
                if page_rect.width > 0 and page_rect.height > 0:
                    w_ratio = img_w / page_rect.width
                    h_ratio = img_h / page_rect.height
                    if w_ratio > 0.8 and h_ratio > 0.8:
                        is_single_full_page = True
            except Exception:
                pass

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

    doc.close()
    return figures


def fetch_arxiv_paper_figures(paper: Dict[str, Any], max_figures: int = 6) -> List[Dict[str, Any]]:
    """arXiv 論文の PDF を一時ダウンロードし、図表（Figure）を抽出する"""
    pdf_url = paper.get("pdf_url")
    if not pdf_url:
        return []

    print(f"  📥 [arXiv PDF] 論文PDFから図表を抽出中: {pdf_url}...")
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp_arxiv_pdfs")
    os.makedirs(temp_dir, exist_ok=True)

    clean_id = paper['arxiv_id'].replace('/', '_').replace('.', '-')
    temp_pdf_path = os.path.join(temp_dir, f"{clean_id}.pdf")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    try:
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
            fallback_img = f"\n\n![文献 p.{f['page']} より抽出された図表]({data_url})\n\n"
            if "## まとめ" in body:
                body = body.replace("## まとめ", f"{fallback_img}## まとめ", 1)
            elif "## で、私" in body:
                body = body.replace("## で、私", f"{fallback_img}## で、私", 1)
            else:
                body += fallback_img
            print(f"  🖼️ 図の自動配置（フォールバック挿入）: p.{f['page']} ({f['width']}x{f['height']})")

    # 未置換・存在しない図のプレースホルダー（LLMの幻覚等）を確実にクリーンアップしてViteビルドエラーを防止
    body = re.sub(r"!\[.*?\]\(\{\{PDF_FIGURE_\d+\}\}\)\n?", "", body)
    body = re.sub(r"\{\{PDF_FIGURE_\d+\}\}", "", body)

    return body


# 後方互換性エイリアス
extract_figures_from_pdf = extract_pdf_figures
