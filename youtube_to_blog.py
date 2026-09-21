#!/usr/bin/env python3
"""
youtube_to_blog.py
==================
YouTube動画のURLから NotebookLM を経由して、
動画内容の理解・要約・論考を含むハイクオリティなブログ記事を自動生成し、
さらに TypeSafe Jev × Gemini による自律推敲ループ（Evaluator-Optimizer）で
合格基準（9.2〜10点以上）までブラッシュアップして yagibrary に保存するツール。

使用例:
  python youtube_to_blog.py "https://www.youtube.com/watch?v=mIpgA0QD7ys"
  python youtube_to_blog.py --refine strings2026
  python youtube_to_blog.py "https://youtu.be/xxxx" --no-optimize
"""

import os
import sys
import re
import json
import time
import subprocess
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv(".env")

from score_post import verify_post_with_jev, DIAGNOSIS_LABELS, extract_post_content, print_report, resolve_file_paths

JST = timezone(timedelta(hours=9))

YAGIBRARY_POSTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "../yagibrary/src/content/posts")
)

_gemini_client = None


def get_gemini_client():
    """Gemini API クライアントの遅延シングルトン初期化"""
    global _gemini_client
    if _gemini_client is None:
        try:
            from google import genai
            _gemini_client = genai.Client()
        except Exception as e:
            raise RuntimeError(f"Gemini Client 初期化エラー: {e}")
    return _gemini_client


def extract_youtube_id(url: str) -> Optional[str]:
    """YouTube URLから動画IDを抽出"""
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"(?:embed\/)([0-9A-Za-z_-]{11})",
        r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def run_nlm_command(cmd_args: list, timeout: int = 120) -> Tuple[int, str, str]:
    """nlm CLI コマンドを実行"""
    full_cmd = ["nlm"] + cmd_args
    proc = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ==============================================================================
# Evaluator-Optimizer リライトエンジン (Gemini)
# ==============================================================================
def rewrite_youtube_post_with_gemini(
    draft_text: str,
    feedback_metrics: Dict[str, Any],
    revision_round: int,
    video_title: str = "",
    youtube_url: str = "",
) -> str:
    """Jev のフィードバックに基づき、動画解説記事を自律推敲・加筆リライト"""
    client = get_gemini_client()
    print(f"\n🔄 [Gemini リライト Round {revision_round}] Jevの診断フィードバックを反映して記事を再推敲中...")

    diagnosis = feedback_metrics.get("diagnosis", "")
    focus_instructions = []

    # 1. 行間・教育的丁寧さ
    if feedback_metrics.get("clarity", 0.0) < 2.0 or feedback_metrics.get("rushed_math_risk", 0.0) >= 0.35 or diagnosis == "need_pedagogical_steps":
        focus_instructions.append(
            "- 【最重要：前提知識と途中計算（行間）の徹底解説】\n"
            "  読者が置いてけぼりになっています！難解な専門用語の圧縮羅列をやめてください。\n"
            "  1. 専門用語や記号（何を意味するか）を初出時に必ず平易に定義してください。\n"
            "  2. 主要な主張や数式について、「なぜその結論になるのか」「どういう論理変形を経ているのか」の途中ステップ（行間）を読者が追体験できるように解説してください。\n"
            "  3. 理工系学部生でも納得できるように、初等的な物理・数学概念（標準的な場の理論や調和振動子、線形代数など）との対比を入れてください。"
        )

    # 2. 筆者オピニオン・独自スタンス
    if feedback_metrics.get("stance", 0.0) < 2.0 or feedback_metrics.get("lack_of_opinion_risk", 0.0) >= 0.35 or diagnosis == "need_sharp_opinion":
        focus_instructions.append(
            "- 【最重要：筆者オピニオンの徹底強化】\n"
            "  客観的な会議レポート・要約に終始しており、「で、筆者（私）はどう考えるか？」が完全に不足しています！\n"
            "  1. 独立した大見出し『## 私（筆者）はどう考えるか？――数理の深淵と残された問い』を必ず設け、最低800〜1,200字かけて熱量高く論じてください。\n"
            "  2. 「どの数理的アイデアに最も知的な美しさ・驚きを感じたか」「この理論の現実の実験・観測とのギャップや課題は何か」「今後10年でどう発展するか」について、一人の研究者・物理ファンとしての鋭い主観的オピニオンを打ち出してください。"
        )

    # 3. 数理の具体性
    if feedback_metrics.get("math_depth", 0.0) < 2.0 or diagnosis == "need_math_details":
        focus_instructions.append(
            "- 【最重要：核心となる数理構造の具体化】\n"
            "  抽象的な美辞麗句（「深遠な影響」「革命的進展」など）を削り、具体的な数理機構"
            "（対称性代数、作用素の交換関係、重力経路積分の幾何、境界CFTの共形ブロックなど）がどう論理的に機能しているのかを明快に解説してください。"
        )

    # 4. 知的好奇心
    if feedback_metrics.get("appeal", 0.0) < 2.0:
        focus_instructions.append(
            "- 【知的好奇心の刺激】\n"
            "  教科書的なまとめを脱し、理論物理の真の美しさとスリル、研究者たちが何に興奮しているのかという『熱気』を鮮やかに伝えてください。"
        )

    if not focus_instructions:
        focus_instructions.append(
            "- 全体的に高いクオリティです。さらに各節の論理展開を磨き、読者を強く引き込む名論考に仕上げてください。"
        )

    guidelines = "\n".join(focus_instructions)

    prompt = f"""
あなたは理論物理学・数理科学の第一線で研究しながら、圧倒的な文章力と教育的配慮で読者を惹きつける一流のサイエンスブロガーです。

以下は、YouTube動画「{video_title}」({youtube_url}) の書き起こしを元に作成された前回のドラフト記事です。
このドラフトに対し、AI採点エージェント（TypeSafe Jev）から厳しい品質改善指示が出ています。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【Jev 診断結果】
・総合スコア: {feedback_metrics.get('total_score', 0.0):.2f} / 12.0
・数理の具体性: {feedback_metrics.get('math_depth', 0.0):.2f} / 3.0
・行間・導出の丁寧さ: {feedback_metrics.get('clarity', 0.0):.2f} / 3.0
・筆者オピニオン: {feedback_metrics.get('stance', 0.0):.2f} / 3.0
・知的好奇心刺激度: {feedback_metrics.get('appeal', 0.0):.2f} / 3.0
・診断ボトルネック: {diagnosis} ({DIAGNOSIS_LABELS.get(diagnosis, '')})
・肩透かしリスク: {feedback_metrics.get('lack_of_opinion_risk', 0.0):.1%}
・置いてけぼりリスク: {feedback_metrics.get('rushed_math_risk', 0.0):.1%}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【今回集中的に改善すべき必須指示】
{guidelines}

【執筆ルール】
1. テキストを太字にする場合は、Markdownの `**` ではなく、必ず HTMLの `<strong>` タグ（例: `<strong>太字テキスト</strong>`）を使用してください。
2. 前回のドラフトに記載されている事実・講演内容・表などの正確な情報は全て活かしてください。
3. 冒頭に # 見出しはつけず、## 見出しから始めてください（タイトルはFrontmatterで設定するため）。
4. 必ず十分な文量（4,000〜7,000字程度）で、行間を親切に埋めた最高の完成稿にしてください。

【前回のドラフト】
{draft_text}
"""

    candidate_models = [
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-2.0-flash",
    ]
    rewritten_text = None
    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            rewritten_text = response.text
            print(f"  ✓ Gemini リライト完了 (モデル: {model_name})")
            break
        except Exception as e:
            print(f"  ⚠️ {model_name} でのエラー: {e}")

    if not rewritten_text:
        print("  ⚠️ リライトに失敗したため、元のドラフトを維持します。")
        return draft_text

    return rewritten_text


def refine_youtube_blog_post(
    initial_draft: str,
    video_title: str = "",
    youtube_url: str = "",
    max_revisions: int = 2,
    verbose: bool = True,
) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """
    Evaluator-Optimizer ループ:
    NotebookLM 初稿 ➔ Jev 採点 ➔ 足切りなら Gemini リライト ➔ Jev 再採点
    """
    current_draft = extract_post_content(initial_draft, strip_frontmatter=True)
    
    # Step 1: 初稿検証
    metrics = verify_post_with_jev(current_draft, round_num=1, verbose=verbose)
    history = [metrics]
    if verbose:
        print_report("Round 1 (NotebookLM 初稿)", metrics)

    if metrics.get("passed", False):
        if verbose:
            print("🎉 初稿で Jev の厳格品質基準をクリアしました！")
        return current_draft, metrics, history

    # Step 2: リライトループ
    round_count = 1
    while not metrics.get("passed", False) and round_count <= max_revisions:
        round_count += 1
        if verbose:
            print(f"\n⚡ 基準未達のため、Jevの改善指示を反映してリライトを実行します (Revision {round_count - 1}/{max_revisions})...")

        current_draft = rewrite_youtube_post_with_gemini(
            draft_text=current_draft,
            feedback_metrics=metrics,
            revision_round=round_count,
            video_title=video_title,
            youtube_url=youtube_url,
        )

        metrics = verify_post_with_jev(current_draft, round_num=round_count, verbose=verbose)
        history.append(metrics)
        if verbose:
            print_report(f"Round {round_count} (Gemini 推敲稿)", metrics)

        if metrics.get("passed", False):
            if verbose:
                print(f"\n🎉 Jev の厳格品質基準をクリアしました！ (Round {round_count})")
            break

    if not metrics.get("passed", False) and verbose:
        print(f"\n⚠️ 最大リビジョン数 ({max_revisions}回) に達しました。現時点で最高品質の原稿を採用します。")

    return current_draft, metrics, history


# ==============================================================================
# yagibrary (Astro) 向けフォーマット整形処理
# ==============================================================================
def format_markdown_for_yagibrary(
    content_body: str,
    youtube_url: str,
    video_id: str,
    source_title: str = "",
    quality_report: Optional[Dict[str, Any]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, str]:
    """
    Markdown本文を yagibrary 仕様に整形
    - title, date, summary, tags の Frontmatter 構築
    - **太字** を <strong>太字</strong> に置換
    - YouTube動画リンクの挿入
    - Evaluator-Optimizer 採点レポートの付加
    """
    lines = content_body.strip().splitlines()
    title = source_title or "YouTube動画解説"
    body_lines = []

    # タイトル行の抽出
    in_title = False
    for line in lines:
        if not in_title and (line.startswith("# ") or line.startswith("## ")):
            candidate_t = line.lstrip("#").strip().replace("*", "").replace("`", "")
            if len(candidate_t) > 5 and not candidate_t.startswith("元動画"):
                title = candidate_t
                in_title = True
                continue
        body_lines.append(line)

    body = "\n".join(body_lines).strip()

    # 太字置換: **text** -> <strong>text</strong>
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)

    # 概要文（summary）の自動抽出（最初の段落）
    summary = ""
    for para in body.split("\n\n"):
        clean_p = para.strip().replace("<strong>", "").replace("</strong>", "").replace("#", "")
        if len(clean_p) > 30 and not clean_p.startswith("-") and not clean_p.startswith("|") and not clean_p.startswith("元動画"):
            summary = clean_p[:150].replace("\n", " ") + "..."
            break
    if not summary:
        summary = f"YouTube動画「{source_title or title}」の要点と核心的な議論をわかりやすく整理した解説記事です。"

    # タグの選定
    tags = ["YouTube解説", "動画要約"]
    title_lower = (title + " " + body[:1000]).lower()
    if any(k in title_lower for k in ["弦理論", "超弦理論", "string", "ホログラフィ", "量子重力"]):
        tags.extend(["物理学", "超弦理論", "数理物理", "ホログラフィ"])
    elif any(k in title_lower for k in ["量子", "量子コンピュータ", "quantum"]):
        tags.extend(["物理学", "量子情報"])
    elif any(k in title_lower for k in ["aws", "cloud", "インフラ", "サーバー"]):
        tags.extend(["AWS", "クラウド", "インフラ"])
    elif any(k in title_lower for k in ["ai", "llm", "エージェント", "機械学習"]):
        tags.extend(["AI", "LLM", "技術解説"])

    now_jst = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:00+09:00")

    # 採点レポートセクション
    meta_section = ""
    if quality_report:
        q = quality_report
        passed_str = "合格" if q.get("passed", False) else "改善完了"
        history_summary = " ➡️ ".join([f"第{h['round']}稿: {h['total_score']}点" for h in (history or [q])])
        meta_section = f"""

---

### 📊 Jev 自律推敲（Evaluator-Optimizer）レポート
- <strong>最終品質スコア</strong>: <code>{q.get('total_score', 0):.2f} / 12.0</code>（判定: <code>{passed_str}</code>）
- <strong>数理・理論の具体性</strong>: <code>{q.get('math_depth', 0):.2f} / 3.0</code>
- <strong>行間・途中計算の丁寧さ</strong>: <code>{q.get('clarity', 0):.2f} / 3.0</code>
- <strong>筆者オピニオン度</strong>: <code>{q.get('stance', 0):.2f} / 3.0</code>
- <strong>知的好奇心刺激度</strong>: <code>{q.get('appeal', 0):.2f} / 3.0</code>
- <strong>肩透かしリスク</strong>: <code>{q.get('lack_of_opinion_risk', 0)*100:.1f}%</code>
- <strong>置いてけぼりリスク</strong>: <code>{q.get('rushed_math_risk', 0)*100:.1f}%</code>
- <strong>自律推敲プロセス</strong>: <code>{history_summary}</code>
"""

    import yaml

    frontmatter_dict = {
        "title": title,
        "date": now_jst,
        "summary": summary,
        "tags": tags,
    }
    frontmatter_yaml = yaml.dump(
        frontmatter_dict,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()

    frontmatter = f"""---
{frontmatter_yaml}
---

## 元動画情報
- <strong>動画タイトル</strong>: {source_title or title}
- <strong>動画URL</strong>: [YouTubeで視聴する（{youtube_url}）]({youtube_url})

---

{body}{meta_section}
"""
    return frontmatter, title, summary


# ==============================================================================
# メインパイプライン
# ==============================================================================
def generate_youtube_blog_post(
    youtube_url: str,
    custom_prompt: Optional[str] = None,
    output_dir: Optional[str] = None,
    optimize: bool = True,
    max_revisions: int = 2,
    verbose: bool = True,
) -> Optional[str]:
    """YouTube動画からブログ記事を生成・自律推敲・保存するメインパイプライン"""
    video_id = extract_youtube_id(youtube_url)
    if not video_id:
        print(f"❌ 無効なYouTube URLです: {youtube_url}", file=sys.stderr)
        return None

    clean_url = f"https://www.youtube.com/watch?v={video_id}"
    if verbose:
        print("\n" + "=" * 64)
        print(" 🎬 YouTube to Blog Post パイプライン (NotebookLM × Jev × Gemini)")
        print(f" 対象URL: {clean_url} (ID: {video_id})")
        print("=" * 64)

    # 1. ノートブック作成
    if verbose:
        print("📓 [1/5] NotebookLM に専用ノートブックを作成中...")
    code, stdout, stderr = run_nlm_command(["notebook", "create", f"YouTube: {video_id}", "--json"])
    if code != 0:
        print(f"❌ ノートブック作成に失敗しました: {stderr}", file=sys.stderr)
        return None

    try:
        nb_info = json.loads(stdout)
        nb_id = nb_info.get("notebook_id") or nb_info.get("id")
    except Exception:
        uuid_match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", stdout)
        nb_id = uuid_match.group(1) if uuid_match else None

    if not nb_id:
        print(f"❌ ノートブックIDの取得に失敗しました: {stdout}", file=sys.stderr)
        return None

    if verbose:
        print(f"   ✓ Notebook ID: {nb_id}")

    # 2. ソース（YouTube動画）の追加
    if verbose:
        print("📥 [2/5] YouTube動画をソースとして取り込み中（字幕・音声インデックス）...")
    code, stdout, stderr = run_nlm_command(["source", "add", nb_id, "--url", clean_url, "--json"], timeout=180)
    if code != 0:
        print(f"❌ ソース追加に失敗しました: {stderr}", file=sys.stderr)
        return None

    source_title = ""
    try:
        src_info = json.loads(stdout)
        source_title = src_info.get("title", "")
    except Exception:
        title_match = re.search(r"Added source:\s*(.+)", stdout)
        if title_match:
            source_title = title_match.group(1).strip()

    if verbose:
        print(f"   ✓ 動画タイトル: {source_title or video_id}")

    # 3. レポート（ブログ記事ドラフト）生成
    if verbose:
        print("✍️ [3/5] NotebookLM で初期ドラフトを執筆中...")

    default_prompt = (
        "このYouTube動画の書き起こしを元に、知的好奇心を持つ読者に向けて、"
        "動画の全体像と核心となるアイデア、議論のハイライト、重要なタイムラインやキーワードを丁寧に解説する"
        "ハイクオリティなブログ記事（Markdown形式）を作成してください。"
        "単なる箇条書きの要約ではなく、話者が伝えたかった熱量や本質、読者が深く理解できる論理的な構成にしてください。"
    )
    prompt_to_use = custom_prompt if custom_prompt else default_prompt

    code, stdout, stderr = run_nlm_command([
        "report", "create", nb_id,
        "--format", "Create Your Own",
        "--prompt", prompt_to_use,
        "--language", "ja",
        "--confirm",
        "--json",
    ])
    if code != 0:
        print(f"❌ レポート生成開始に失敗しました: {stderr}", file=sys.stderr)
        return None

    # 4. 生成完了のポーリング待機
    if verbose:
        print("⏳ [4/5] NotebookLM のドラフト完成を待機中...")

    max_wait = 180
    start_time = time.time()
    report_completed = False

    while time.time() - start_time < max_wait:
        time.sleep(6)
        code, stdout, _ = run_nlm_command(["studio", "status", nb_id, "--json"])
        if code == 0:
            try:
                status_list = json.loads(stdout)
                for item in status_list:
                    if item.get("type") == "report":
                        if item.get("status") in ["completed", "complete", "ready"]:
                            report_completed = True
                            break
            except Exception:
                if "completed" in stdout.lower():
                    report_completed = True
                    break
        if report_completed:
            break

    # 5. ダウンロード
    tmp_path = os.path.join(os.path.dirname(__file__), f"temp_report_{video_id}.md")
    code, stdout, stderr = run_nlm_command(["download", "report", nb_id, "--output", tmp_path])
    if code != 0 or not os.path.isfile(tmp_path):
        print(f"❌ レポートのダウンロードに失敗しました: {stderr}", file=sys.stderr)
        return None

    with open(tmp_path, "r", encoding="utf-8") as f:
        raw_report = f.read()

    try:
        os.remove(tmp_path)
    except OSError:
        pass

    # 6. Evaluator-Optimizer による自律推敲
    final_body = raw_report
    final_metrics = None
    history = None

    if optimize:
        if verbose:
            print("\n🔍 [5/5] TypeSafe Jev × Gemini による自律推敲ループ（Evaluator-Optimizer）を開始...")
        final_body, final_metrics, history = refine_youtube_blog_post(
            initial_draft=raw_report,
            video_title=source_title,
            youtube_url=clean_url,
            max_revisions=max_revisions,
            verbose=verbose,
        )

    # Astro向け整形
    final_post, title, summary = format_markdown_for_yagibrary(
        final_body, clean_url, video_id, source_title=source_title,
        quality_report=final_metrics, history=history,
    )

    # 保存先ファイルの決定
    save_dir = output_dir if output_dir else YAGIBRARY_POSTS_DIR
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    filename = f"{today_str}-youtube-{video_id}.md"
    target_filepath = os.path.join(save_dir, filename)

    os.makedirs(save_dir, exist_ok=True)
    with open(target_filepath, "w", encoding="utf-8") as f:
        f.write(final_post)

    if verbose:
        print("\n" + "=" * 64)
        print(f" 🎉 ブログ記事の生成・推敲が完了しました！")
        print(f" 記事タイトル: {title}")
        print(f" 保存先: {target_filepath}")
        if final_metrics:
            print(f" 最終Jevスコア: {final_metrics.get('total_score')}/12.0 (判定: {'合格' if final_metrics.get('passed') else '改善完了'})")
        print("=" * 64 + "\n")

    return target_filepath


def refine_existing_file(file_path: str, max_revisions: int = 2) -> Optional[str]:
    """既存のブログ記事ファイルを読み込んで Evaluator-Optimizer で推敲・上書き保存"""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # YouTube URL の抽出（もしあれば）
    yt_match = re.search(r"https://www\.youtube\.com/watch\?v=([0-9A-Za-z_-]{11})", content)
    yt_url = yt_match.group(0) if yt_match else ""
    video_id = yt_match.group(1) if yt_match else "video"

    # タイトルの抽出
    title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
    title = title_match.group(1).strip().strip('"\'') if title_match else ""

    print("\n" + "=" * 64)
    print(f" 🚀 既存記事の自律推敲（Evaluator-Optimizer）")
    print(f" ファイル: {file_path}")
    print(f" タイトル: {title}")
    print("=" * 64)

    refined_body, final_metrics, history = refine_youtube_blog_post(
        initial_draft=content,
        video_title=title,
        youtube_url=yt_url,
        max_revisions=max_revisions,
        verbose=True,
    )

    final_post, updated_title, summary = format_markdown_for_yagibrary(
        refined_body, yt_url, video_id, source_title=title,
        quality_report=final_metrics, history=history,
    )

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_post)

    print(f"\n✅ 記事を上書き更新しました: {file_path}")
    return file_path


def main():
    parser = argparse.ArgumentParser(
        description="YouTube動画から NotebookLM × TypeSafe Jev × Gemini でブログ記事を自動生成・推敲するツール"
    )
    parser.add_argument("url_or_file", nargs="?", help="YouTube動画のURL、または既存記事のファイル名/パス（--refine時）")
    parser.add_argument(
        "--refine", "-r",
        action="store_true",
        help="指定された既存記事ファイルをJev×Geminiで自律推敲・改善する",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Jev×Geminiの推敲ループをスキップし、NotebookLMの初稿をそのまま保存する",
    )
    parser.add_argument(
        "--max-revisions",
        type=int,
        default=2,
        help="最大リライト回数（デフォルト: 2）",
    )
    parser.add_argument(
        "--prompt", "-p",
        help="記事生成のカスタムプロンプト（例: '理系学部生向けにわかりやすく解説して'）",
        default=None,
    )
    parser.add_argument(
        "--output-dir", "-o",
        help="記事の保存先ディレクトリ（デフォルト: yagibrary/src/content/posts）",
        default=None,
    )

    args = parser.parse_args()

    if not args.url_or_file:
        parser.print_help()
        sys.exit(0)

    if args.refine:
        resolved = resolve_file_paths(args.url_or_file)
        if not resolved:
            print(f"❌ ファイルが見つかりません: {args.url_or_file}", file=sys.stderr)
            sys.exit(1)
        for f in resolved:
            refine_existing_file(f, max_revisions=args.max_revisions)
    else:
        generate_youtube_blog_post(
            youtube_url=args.url_or_file,
            custom_prompt=args.prompt,
            output_dir=args.output_dir,
            optimize=not args.no_optimize,
            max_revisions=args.max_revisions,
            verbose=True,
        )


if __name__ == "__main__":
    main()
