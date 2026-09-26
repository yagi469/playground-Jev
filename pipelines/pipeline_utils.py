import os
import re
from typing import Optional


def resolve_output_filepath(
    base_slug: str,
    target_dir: str,
    pages: Optional[str] = None,
    chapter: Optional[str] = None,
    custom_filename: Optional[str] = None,
) -> str:
    """ページ番号・章番号サフィックスを付与し、既存ファイルがある場合は連番 (-2, -3, ...) をつけて

    重複しない安全なファイルパス (例: /path/to/arxiv-2108-09400v2-p1-17.md) を返す。
    """
    if custom_filename:
        filename = custom_filename if custom_filename.endswith(".md") else f"{custom_filename}.md"
        safe_slug = os.path.splitext(filename)[0]
    else:
        safe_slug = re.sub(r"[^a-zA-Z0-9_\-]+", "-", base_slug).strip("-_").lower()

        # 章番号の付与
        if chapter:
            # "Chapter 3" -> "ch3", "第3章" -> "ch3", "3" -> "ch3" など
            ch_match = re.search(r'(?:chapter|ch|第)?\s*([0-9]+)', str(chapter), re.IGNORECASE)
            if ch_match:
                ch_tag = f"ch{ch_match.group(1)}"
            else:
                ch_tag = re.sub(r"[^a-zA-Z0-9_\-]+", "-", str(chapter)).strip("-_").lower()[:20]
            if ch_tag and ch_tag not in safe_slug:
                safe_slug = f"{safe_slug}-{ch_tag}"

        # ページ番号の付与
        if pages:
            # pages: "1-17" -> "p1-17", "15" -> "p15"
            safe_p = re.sub(r"[^a-zA-Z0-9_\-]+", "-", str(pages)).strip("-_").lower()
            p_tag = f"p{safe_p}" if not safe_p.startswith("p") else safe_p
            if p_tag and p_tag not in safe_slug:
                safe_slug = f"{safe_slug}-{p_tag}"

        filename = f"{safe_slug}.md"

    out_file_path = os.path.join(target_dir, filename)

    # 重複がある場合はインデックスを付与（例: slug-2.md, slug-3.md）
    counter = 1
    while os.path.exists(out_file_path):
        counter += 1
        filename = f"{safe_slug}-{counter}.md"
        out_file_path = os.path.join(target_dir, filename)

    return out_file_path
