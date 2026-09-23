"""
core/book_queue.py
==================
書籍キュー（book_queue.json）の読み込み、保存、次回連載タスクの特定
"""

import os
import json
from typing import Dict, Any, Optional
from config import DEFAULT_BOOK_QUEUE_PATH


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
    """次に執筆・投稿すべき未公開章（pending）を探索して返す"""
    order = queue_data.get("queue_order", [])
    active_id = queue_data.get("active_book_id")
    if active_id and active_id in order:
        idx = order.index(active_id)
        search_order = order[idx:] + order[:idx]
    else:
        search_order = order

    for book_id in search_order:
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
