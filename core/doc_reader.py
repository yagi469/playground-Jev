"""
core/doc_reader.py
==================
ローカルドキュメント（PDF / Markdown / テキスト）のパス解決、PDFページ切り出し、
目次解析・ノンブルオフセット推定、自動スリム化、メタデータ抽出
"""

import os
import io
import re
import json
from typing import List, Dict, Any, Optional, Tuple

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

from config import init_gemini_client
from core.figure_extractor import extract_pdf_figures
from score_post import classify_genre_with_jev


def resolve_document_path(file_path: str) -> str:
    """ローカルファイルパスを解決する（OS間の区切り文字の違い、カレント、yagibrary、docs配下の再帰的探索）"""
    normalized = file_path.replace("\\", "/").strip()
    clean_rel = re.sub(r"^(?:docs/|yagibrary/docs/|yagibrary/)", "", normalized)

    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.abspath(normalized),
        os.path.join(script_dir, normalized),
        os.path.join(script_dir, "yagibrary", normalized),
        os.path.join(script_dir, "yagibrary/docs", clean_rel),
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

    target_basename = os.path.basename(normalized)
    possible_names = [target_basename]
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


def detect_pdf_nombre_offset(
    pdf_path: str,
    target_printed_page: int,
    total_pages: int,
) -> int:
    """
    書籍の印刷ページ番号（ノンブル）とPDFの物理通し番号の差分（offset）を自動推定する。
    """
    client = init_gemini_client()

    probe_candidates = []
    if 1 <= target_printed_page <= total_pages:
        probe_candidates.append(target_printed_page)
    if 1 <= target_printed_page - 5 <= total_pages:
        probe_candidates.append(target_printed_page - 5)
    if 1 <= target_printed_page + 5 <= total_pages:
        probe_candidates.append(target_printed_page + 5)
    for fallback_p in [60, 50, 40]:
        if fallback_p <= total_pages and fallback_p not in probe_candidates:
            probe_candidates.append(fallback_p)

    reader = PdfReader(pdf_path)
    for probe_p in probe_candidates:
        try:
            writer = PdfWriter()
            writer.add_page(reader.pages[probe_p - 1])
            buf = io.BytesIO()
            writer.write(buf)
            page_bytes = buf.getvalue()
            if not page_bytes:
                continue

            from google.genai import types
            part = types.Part.from_bytes(data=page_bytes, mime_type="application/pdf")
            prompt = f"""添付のPDFページは、ファイル全体の先頭から数えて【第 {probe_p} ページ目】（PDF物理ページ）です。
このページの下部、ヘッダー、または隅（柱・ノンブル部分）に印刷されている【書籍自体のページ番号】（1つの整数、例: 330 や 24）を読み取ってください。

以下のJSON形式のみを出力してください（Markdownの ```json ... ``` で囲んでください）:
{{
  "printed_page": 330
}}
※もし扉絵、白紙、見出しページなどで印刷ページ番号が全く印字されていない場合は、{{"printed_page": null}} としてください。
"""
            print(f"   🔍 [ノンブル照合] PDF物理第 {probe_p} ページをプローブして書籍ノンブルとのオフセットを算出中...")
            res = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[part, prompt]
            )
            raw = res.text
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            data = json.loads(m.group(1)) if m else json.loads(raw.strip())
            printed_p = data.get("printed_page")
            if printed_p is not None and isinstance(printed_p, int):
                offset = probe_p - printed_p
                print(f"   📏 [ノンブル照合成功] PDF物理第 {probe_p} ページ ＝ 書籍ノンブル p.{printed_p} → オフセット: +{offset} ページ")
                return offset
        except Exception:
            continue

    print("   ℹ️ ノンブルの自動検出がスキップされました（オフセット: 0として処理）")
    return 0


def apply_offset_to_pages_str(pages_str: str, offset: int, total_pages: int) -> str:
    """ページ範囲文字列（例: '366-378, 380'）にオフセットを加算して新しい範囲文字列を返す"""
    if not offset or not pages_str:
        return pages_str
    parts = [p.strip() for p in pages_str.split(",") if p.strip()]
    shifted_parts = []
    for part in parts:
        if "-" in part:
            s_str, e_str = part.split("-", 1)
            try:
                s = max(1, min(int(s_str.strip()) + offset, total_pages))
                e = max(s, min(int(e_str.strip()) + offset, total_pages))
                shifted_parts.append(f"{s}-{e}")
            except ValueError:
                shifted_parts.append(part)
        else:
            try:
                p = max(1, min(int(part) + offset, total_pages))
                shifted_parts.append(str(p))
            except ValueError:
                shifted_parts.append(part)
    res = ", ".join(shifted_parts)
    print(f"   📏 [オフセット手動適用] 指定ページ p.{pages_str} に offset +{offset} を加算 → PDF物理 p.{res}")
    return res


def resolve_chapter_pages_from_toc(
    pdf_path: str,
    chapter_hint: str,
    total_pages: int,
    manual_offset: Optional[int] = None,
) -> Optional[str]:
    """
    大部数PDF（書籍・マニュアル等）において、目次（TOC）または目次スキャンページから、
    指定された章・テーマのページ範囲を自動特定し、
    書籍の印刷ノンブルとPDF物理ページ番号のオフセットを補正した物理ページ範囲を返す。
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

    # 2. しおりが無い場合、先頭の目次ページ（p.4〜p.22）を Gemini に解析させてページ番号を特定
    try:
        from google.genai import types
        reader = PdfReader(pdf_path)
        toc_writer = PdfWriter()
        start_toc = min(3, total_pages - 1)
        end_toc = min(22, total_pages)
        for i in range(start_toc, end_toc):
            toc_writer.add_page(reader.pages[i])

        buf = io.BytesIO()
        toc_writer.write(buf)
        toc_bytes = buf.getvalue()

        if len(toc_bytes) > 0:
            client = init_gemini_client()
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
            print(f"   🔍 [目次AIスキャン] 目次ページ (p.{start_toc + 1}-{end_toc}) を解析して 『{chapter_hint}』 の掲載ページを探索中...")
            res = client.models.generate_content(
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
                book_start_p = int(toc_data.get("start_page", 1))
                book_end_p = int(toc_data.get("end_page", book_start_p + 15))

                if manual_offset is not None:
                    offset = manual_offset
                else:
                    offset = detect_pdf_nombre_offset(pdf_path, book_start_p, total_pages)

                real_start_p = max(1, min(book_start_p + offset, total_pages))
                real_end_p = max(real_start_p, min(book_end_p + offset, total_pages))

                if real_end_p - real_start_p > 35:
                    real_end_p = real_start_p + 35

                range_str = f"{real_start_p}-{real_end_p}"
                print(f"   🎯 [目次AIスキャン＆オフセット補正成功] 『{chapter_hint}』の掲載ページを自動特定しました:")
                print(f"      - 書籍ノンブル: p.{book_start_p}-{book_end_p}")
                print(f"      - オフセット: +{offset} ページ")
                print(f"      - PDF物理ページ: p.{range_str} (見出し: {toc_data.get('chapter_title')})")
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
    offset: Optional[int] = None,
) -> Dict[str, Any]:
    """
    ローカルの PDF または Markdown / Text ファイルを読み込み、Gemini 用のコンテンツオブジェクトと
    基本メタデータ（タイトル・要約・ジャンル・サブ領域など）を構造化して返す。
    """
    client = init_gemini_client()

    resolved_path = resolve_document_path(file_path)
    file_name = os.path.basename(resolved_path)
    ext = os.path.splitext(file_name)[-1].lower()

    print(f"\n📂 [ローカルファイル読解] ファイル: {resolved_path} (拡張子: {ext})")
    if chapter_hint:
        print(f"   🎯 対象章/テーマ: {chapter_hint}")
    if offset is not None:
        print(f"   📏 指定オフセット: +{offset} ページ")

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
        total_pages = len(PdfReader(resolved_path).pages)

        effective_pages_str = pages_str
        if not effective_pages_str and chapter_hint and total_pages > 40:
            auto_pages = resolve_chapter_pages_from_toc(
                resolved_path, chapter_hint, total_pages, manual_offset=offset
            )
            if auto_pages:
                effective_pages_str = auto_pages
        elif effective_pages_str and offset is not None:
            effective_pages_str = apply_offset_to_pages_str(effective_pages_str, offset, total_pages)

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
  "genre": "business（ビジネス・マネジメント・組織論・経済）, tech（ソフトウェア・システム設計・工学）, physics（数理物理・理論物理・量子・科学論文）, classics（歴史・古典文学・哲学・英雄譚・思想書）, general（一般教養・その他）のいずれか1つを必ず選択",
  "categories": ["ドキュメント内容に即した適切なカテゴリタグ3〜4個（例: マネジメント, 組織論, 生産性 / 場の量子論, 超弦理論 / アーキテクチャ, クラウド 等）"],
  "subfield": "具体的な専門分野やテーマ（例: 組織マネジメント, 生産管理, カイラル代数, 分散システム 等）",
  "chapter_pages": "指定された章やテーマ（{chapter_hint or '指定なし'}）が論じられているPDF内のページ範囲（例: '27-60'、不明な場合は空文字）"
}}
"""
        try:
            res = client.models.generate_content(
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

        # 2. 対象章のページ範囲が特定された場合、執筆用PDFデータと図表抽出をスリム化
        if not pages_str and meta.get("chapter_pages"):
            ch_pages = str(meta["chapter_pages"]).strip()
            if re.match(r"^\d+\s*-\s*\d+$", ch_pages) or ch_pages.isdigit():
                effective_pages_str = ch_pages
                try:
                    slim_bytes, slim_label = extract_pdf_pages_bytes(resolved_path, effective_pages_str)
                    doc_info["pdf_part"] = types.Part.from_bytes(data=slim_bytes, mime_type="application/pdf")
                    doc_info["page_label"] = slim_label
                    print(f"   ✂️ [自動スリム化] 執筆対象を特定章のページ範囲に最適化: {slim_label} ({len(slim_bytes):,} bytes)")
                except Exception as e:
                    print(f"   ⚠️ PDF自動スリム化失敗（フォールバック継続）: {e}")

        pdf_figures = extract_pdf_figures(
            resolved_path,
            pages_str=effective_pages_str,
            max_figures=12,
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
  "genre": "business（ビジネス・マネジメント・組織論・経済）, tech（ソフトウェア・システム設計・工学）, physics（数理物理・理論物理・量子・科学論文）, classics（歴史・古典文学・哲学・英雄譚・思想書）, general（一般教養・その他）のいずれか1つを必ず選択",
  "categories": ["文書内容に即した適切なカテゴリタグ3〜4個"],
  "subfield": "具体的な専門分野やテーマ"
}}

【文書本文（先頭抜粋）】
{text_content[:8000]}
"""
        try:
            res = client.models.generate_content(
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

    if not genre or genre == "auto":
        summary_sample = doc_info.get("summary", "") or doc_info.get("text_content", "")[:1500]
        detected_genre = classify_genre_with_jev(
            title=doc_info.get("title", file_name),
            summary_or_text=summary_sample,
        )
        doc_info["genre"] = detected_genre
    else:
        doc_info["genre"] = genre

    return doc_info
