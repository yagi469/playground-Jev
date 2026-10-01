"""
core/book_queue.py
==================
書籍キュー（book_queue.json）の読み込み、保存、次回連載タスクの特定
"""

import os
import json
from datetime import datetime
from typing import Dict, Any, Optional
from config import DEFAULT_BOOK_QUEUE_PATH, JST


def load_book_queue(queue_path: str = DEFAULT_BOOK_QUEUE_PATH) -> Dict[str, Any]:
    """書籍キューファイルを読み込む"""
    if not os.path.exists(queue_path):
        raise FileNotFoundError(f"書籍キューファイルが見つかりません: {queue_path}")
    with open(queue_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_book_queue(queue_data: Dict[str, Any], queue_path: str = DEFAULT_BOOK_QUEUE_PATH):
    """書籍キューファイルをアトミックに保存"""
    tmp_path = f"{queue_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(queue_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, queue_path)


def get_next_queue_task(queue_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """次に執筆・投稿すべき未公開章（pending）を探索して返す（queue_order の優先順）"""
    order = queue_data.get("queue_order", [])

    for book_id in order:
        book = queue_data.get("books", {}).get(book_id)
        if not book:
            continue
        chapters = book.get("chapters", [])
        for ch in chapters:
            if ch.get("status") == "pending":
                return {
                    "book_id": book_id,
                    "book": book,
                    "chapter": ch,
                }
    return None


def find_chapter_in_queue(
    file_path: str,
    pages: Optional[str] = None,
    chapter: Optional[str] = None,
    queue_path: str = DEFAULT_BOOK_QUEUE_PATH,
    queue_data: Optional[Dict[str, Any]] = None,
) -> Optional[tuple]:
    """
    指定されたファイルパス、ページ、または章ヒントに一致するキュー内の
    (book_id, book, matched_chapter) を検索して返す。
    """
    if queue_data is None:
        if not os.path.exists(queue_path):
            return None
        try:
            queue_data = load_book_queue(queue_path)
        except Exception:
            return None

    import re
    norm_input = os.path.basename(file_path).strip().lower()

    # 1. 書籍の照合
    matched_book_id = None
    matched_book = None

    for book_id, book in queue_data.get("books", {}).items():
        candidates = []
        book_fp = book.get("file_path", "")
        if book_fp:
            candidates.append(os.path.basename(book_fp).strip().lower())
        candidates.append(book_id.strip().lower())
        if book.get("slug"):
            candidates.append(book.get("slug", "").strip().lower())
        if book.get("title"):
            candidates.append(book.get("title", "").strip().lower())

        for cand in candidates:
            if cand and (cand == norm_input or cand in norm_input or norm_input in cand):
                matched_book_id = book_id
                matched_book = book
                break

        if not matched_book:
            for ch in book.get("chapters", []):
                ch_fp = ch.get("file_path", "")
                if ch_fp and os.path.basename(ch_fp).strip().lower() == norm_input:
                    matched_book_id = book_id
                    matched_book = book
                    break

        if matched_book:
            break

    if not matched_book:
        return None

    chapters = matched_book.get("chapters", [])
    matched_ch = None

    # 2. 章の照合
    # (A) pages 指定による照合
    if pages:
        norm_pages = pages.replace(" ", "")
        for ch in chapters:
            if ch.get("pages") and ch.get("pages").replace(" ", "") == norm_pages:
                matched_ch = ch
                break

    # (B) chapter 指定による照合
    if not matched_ch and chapter:
        ch_str = chapter.strip().lower()

        # B-1. タイトル一致
        for ch in chapters:
            ch_title = ch.get("title", "").strip().lower()
            if ch_title and (ch_str in ch_title or ch_title in ch_str):
                matched_ch = ch
                break

        # B-2. 章番号で照合（例: "第1章", "Chapter 2", "3", "0"）
        if not matched_ch:
            num_match = re.search(r'(?:chapter|ch|第)?\s*(\d+)', ch_str, re.IGNORECASE)
            target_num = int(num_match.group(1)) if num_match else None

            if target_num is not None:
                for ch in chapters:
                    if str(ch.get("chapter")) == str(target_num):
                        matched_ch = ch
                        break

    # (C) 単一ファイル登録（チャプター個別 file_path）の照合
    if not matched_ch:
        for ch in chapters:
            ch_fp = ch.get("file_path", "")
            if ch_fp and os.path.basename(ch_fp).strip().lower() == norm_input:
                matched_ch = ch
                break

    # (D) pages/chapter 指定がなく、章が1つだけの場合、または先頭の pending 章
    if not matched_ch and not pages and not chapter:
        pending_chapters = [c for c in chapters if c.get("status") == "pending"]
        if len(chapters) == 1:
            matched_ch = chapters[0]
        elif pending_chapters:
            matched_ch = pending_chapters[0]

    if not matched_ch:
        return None

    return matched_book_id, matched_book, matched_ch


def get_chapter_pages_from_queue(
    file_path: str,
    chapter: str,
    queue_path: str = DEFAULT_BOOK_QUEUE_PATH,
) -> Optional[tuple]:
    """
    キューから指定された章のページ範囲 (pages_str, chapter_title) を即座に取得する。
    """
    found = find_chapter_in_queue(file_path=file_path, chapter=chapter, queue_path=queue_path)
    if found:
        book_id, book, ch = found
        pages = ch.get("pages")
        if pages:
            return pages, ch.get("title", "")
    return None


def sync_queue_on_file_published(
    file_path: str,
    created_post_file: str,
    pages: Optional[str] = None,
    chapter: Optional[str] = None,
    queue_path: str = DEFAULT_BOOK_QUEUE_PATH,
) -> Optional[Dict[str, Any]]:
    """
    個別ファイル指定実行時に、指定ファイル・ページ・章情報がキューに存在する場合、
    該当する章を published に更新する。
    """
    if not os.path.exists(queue_path):
        return None

    try:
        queue_data = load_book_queue(queue_path)
    except Exception as e:
        print(f" ⚠️ [Book Queue Sync] キュー読込エラー: {e}")
        return None

    found = find_chapter_in_queue(
        file_path=file_path,
        pages=pages,
        chapter=chapter,
        queue_path=queue_path,
        queue_data=queue_data,
    )

    if not found:
        return None

    matched_book_id, matched_book, matched_ch = found
    post_filename = os.path.basename(created_post_file)
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    matched_ch["status"] = "published"
    matched_ch["post_file"] = post_filename
    matched_ch["published_at"] = today_str

    try:
        save_book_queue(queue_data, queue_path)
        ch_label = matched_ch.get("title") or f"第{matched_ch.get('chapter')}章"
        print(
            f"\n✅ [Book Queue Sync] キューを自動更新しました: "
            f"書籍 '{matched_book.get('title')}' / {ch_label} -> published ({post_filename})"
        )
        return {"book_id": matched_book_id, "chapter": matched_ch}
    except Exception as e:
        print(f" ⚠️ [Book Queue Sync] キュー保存失敗: {e}")
        return None
