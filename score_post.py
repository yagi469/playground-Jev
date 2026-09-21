#!/usr/bin/env python3
"""
score_post.py
=============
ブログ記事（Markdown / テキスト）に対して TypeSafe (Jev / System One) を用いて、
「数理的深度・行間の丁寧さ・筆者オピニオン・知的好奇心刺激度」を多面採点・品質評価するCLIツール & モジュール。

使用例:
  python score_post.py ../yagibrary/src/content/posts/2026-09-21-arxiv-2609-19075v1.md
  python score_post.py post1.md post2.md --json
  python score_post.py --latest
"""

import os
import sys
import glob
import json
import argparse
from typing import Dict, Any, Optional, List, Tuple
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv(".env.local")
load_dotenv(".env")

try:
    from typesafe_sdk import TypeSafeClient, Choice, Score, Noul
except ImportError:
    print("❌ Error: typesafe_sdk がインストールされていません。pip install typesafe-sdk を実行してください。")
    sys.exit(1)

_typesafe_client: Optional[TypeSafeClient] = None


def get_typesafe_client() -> TypeSafeClient:
    """TypeSafeClient シングルトンの取得"""
    global _typesafe_client
    if _typesafe_client is None:
        api_key = os.getenv("TYPESAFE_API_KEY")
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY が環境変数または .env.local / .env に設定されていません。")
        _typesafe_client = TypeSafeClient(api_key=api_key)
    return _typesafe_client


def extract_post_content(raw_text: str, strip_frontmatter: bool = True) -> str:
    """
    Markdown ファイルから Frontmatter（--- ... ---）を処理し、
    タイトルと本文を整えて評価用テキストを抽出する。
    """
    if not strip_frontmatter:
        return raw_text

    raw_text = raw_text.strip()
    if raw_text.startswith("---"):
        parts = raw_text.split("---", 2)
        if len(parts) >= 3:
            frontmatter_raw = parts[1]
            body = parts[2].strip()

            # Frontmatter から title を抽出して本文先頭に補う
            title = ""
            for line in frontmatter_raw.splitlines():
                if line.strip().startswith("title:"):
                    title = line.split("title:", 1)[1].strip().strip('"\'')
                    break

            if title:
                return f"# {title}\n\n{body}"
            return body

    return raw_text


def verify_post_with_jev(
    post_content: str,
    round_num: int = 1,
    client: Optional[TypeSafeClient] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    記事テキストに対して Jev (System One) による厳格な多面採点・品質評価を実行する。
    daily_paper_blogger.py と完全互換のインターフェース。
    """
    if client is None:
        client = get_typesafe_client()

    if verbose:
        print(f"\n⚡ [TypeSafe Jev] 記事の品質を検証中 (Round {round_num})...")

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
        res = client.system_one(state={"post": post_content}, questions=questions)
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

        # 高品質ボーナス判定
        high_quality_passed = (
            (total_score >= 10.0)
            and (clarity >= 2.5)
            and (diagnosis == "high_quality")
            and (risk_opinion < 0.40)
            and (risk_rushed < 0.45)
        )

        passed = base_passed or high_quality_passed

        return {
            "round": round_num,
            "math_depth": round(math_depth, 2),
            "clarity": round(clarity, 2),
            "stance": round(stance, 2),
            "appeal": round(appeal, 2),
            "total_score": round(total_score, 2),
            "diagnosis": diagnosis,
            "lack_of_opinion_risk": round(risk_opinion, 3),
            "rushed_math_risk": round(risk_rushed, 3),
            "passed": passed,
        }
    except Exception as e:
        if verbose:
            print(f"⚠️ Jev 検証エラー: {e}")
        return {
            "round": round_num,
            "math_depth": 2.0,
            "clarity": 2.0,
            "stance": 2.0,
            "appeal": 2.0,
            "total_score": 8.0,
            "diagnosis": "error",
            "lack_of_opinion_risk": 0.2,
            "rushed_math_risk": 0.2,
            "passed": False,
            "error": str(e),
        }


DIAGNOSIS_LABELS = {
    "need_pedagogical_steps": "前提知識・記号の定義不足、または数式の導出ステップ（行間）の補強が必要",
    "need_math_details": "核心となる数理構造・代数・幾何のロジックの具体的解説が必要",
    "need_sharp_opinion": "筆者オピニオンの鮮明化・独自の問いや批判的考察の深掘りが必要",
    "avoid_shallow_metaphors": "安易な日常たとえ話の排除・数理の本質的な美しさに徹するべき",
    "high_quality": "高水準に調和（数理・行間・オピニオンが極めて高いクオリティ）",
}


def _render_bar(score: float, max_score: float = 3.0, width: int = 10) -> str:
    """スコアを視覚的なプログレスバーで表現"""
    ratio = max(0.0, min(1.0, score / max_score))
    filled = int(round(ratio * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


def print_report(file_label: str, result: Dict[str, Any]):
    """見やすい診断レポートをターミナルに出力"""
    passed = result.get("passed", False)
    status_str = "✅ 合格 (Pass)" if passed else "⚠️ 改善要 (Needs Work)"
    
    print("\n" + "=" * 64)
    print(f" 📊 Jev 記事品質診断レポート")
    print(f" 対象: {file_label}")
    print("=" * 64)
    
    m_depth = result.get("math_depth", 0.0)
    clarity = result.get("clarity", 0.0)
    stance = result.get("stance", 0.0)
    appeal = result.get("appeal", 0.0)
    total = result.get("total_score", 0.0)
    diag = result.get("diagnosis", "")
    diag_desc = DIAGNOSIS_LABELS.get(diag, diag)
    risk_op = result.get("lack_of_opinion_risk", 0.0)
    risk_ru = result.get("rushed_math_risk", 0.0)

    print(f" ・数理・理論の具体性   : {_render_bar(m_depth)} {m_depth:4.2f} / 3.0  (基準: >= 2.0)")
    print(f" ・行間・導出の丁寧さ   : {_render_bar(clarity)} {clarity:4.2f} / 3.0  (基準: >= 2.0)")
    print(f" ・筆者オピニオンスタンス: {_render_bar(stance)} {stance:4.2f} / 3.0  (基準: >= 2.0)")
    print(f" ・知的好奇心刺激度     : {_render_bar(appeal)} {appeal:4.2f} / 3.0  (基準: >= 2.0)")
    print("-" * 64)
    print(f" 総合スコア             : {total:5.2f} / 12.0  (判定: {status_str})")
    print(f" 最大の改善診断         : {diag}")
    print(f"                          └ {diag_desc}")
    print(f" 肩透かしリスク         : {risk_op:.1%} (基準: < 38%)")
    print(f" 置いてけぼりリスク     : {risk_ru:.1%} (基準: < 40%)")
    if "error" in result:
        print(f" ⚠️ エラー詳細           : {result['error']}")
    print("=" * 64 + "\n")


def resolve_file_paths(target: str) -> List[str]:
    """与えられた文字列から実在するファイルを柔軟に探索し、マッチする全ファイルのリストを返す"""
    matched: List[str] = []

    # 1. そのままのパスで存在するか
    if os.path.isfile(target):
        matched.append(os.path.abspath(target))

    # 2. playground-Jev 相対または yagibrary 相対
    candidates = [
        os.path.join(os.path.dirname(__file__), target),
        os.path.join(os.path.dirname(__file__), "../yagibrary/src/content/posts", target),
        os.path.join(os.path.dirname(__file__), "../yagibrary/src/content/posts", f"{target}.md"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.path.abspath(c) not in matched:
            matched.append(os.path.abspath(c))

    # 3. yagibrary の posts 内で部分一致・ワイルドカード検索
    posts_dir = os.path.join(os.path.dirname(__file__), "../yagibrary/src/content/posts")
    if os.path.isdir(posts_dir):
        query = target if ("*" in target or "?" in target) else f"*{target}*"
        if not query.endswith(".md"):
            query_patterns = [query, f"{query}.md"]
        else:
            query_patterns = [query]

        for qp in query_patterns:
            matches = glob.glob(os.path.join(posts_dir, qp))
            for m in matches:
                abs_m = os.path.abspath(m)
                if os.path.isfile(abs_m) and abs_m not in matched:
                    matched.append(abs_m)

    matched.sort()
    return matched


def get_latest_post_path() -> Optional[str]:
    """yagibrary/src/content/posts 配下で最新更新された Markdown ファイルを取得"""
    posts_dir = os.path.join(os.path.dirname(__file__), "../yagibrary/src/content/posts")
    if not os.path.isdir(posts_dir):
        return None
    md_files = glob.glob(os.path.join(posts_dir, "*.md"))
    if not md_files:
        return None
    return max(md_files, key=os.path.getmtime)


def score_file(file_path: str, strip_frontmatter: bool = True, verbose: bool = True) -> Dict[str, Any]:
    """指定されたファイルを読み込んで採点を行う"""
    with open(file_path, "r", encoding="utf-8") as f:
        raw_text = f.read()
    
    clean_text = extract_post_content(raw_text, strip_frontmatter=strip_frontmatter)
    result = verify_post_with_jev(clean_text, round_num=1, verbose=verbose)
    result["file"] = file_path
    result["char_count"] = len(clean_text)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="TypeSafe Jev によるブログ記事の多面品質採点・評価スクリプト"
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="採点する記事のファイルパス（複数指定可、yagibrary内のファイル名だけでも部分一致で検出可能）",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="yagibrary 内の最新記事を自動選択して採点する",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Frontmatter（--- ... ---）を除去せず、ファイルの内容をそのまま評価対象にする",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="採点結果を整形されたJSON形式で標準出力に出力する",
    )

    args = parser.parse_args()

    targets: List[str] = []
    if args.latest:
        latest = get_latest_post_path()
        if not latest:
            print("❌ エラー: yagibrary 内の記事が見つかりませんでした。", file=sys.stderr)
            sys.exit(1)
        targets.append(latest)
    elif args.files:
        for f_arg in args.files:
            resolved_list = resolve_file_paths(f_arg)
            if not resolved_list:
                print(f"❌ エラー: ファイルが見つかりません: {f_arg}", file=sys.stderr)
                sys.exit(1)
            for r in resolved_list:
                if r not in targets:
                    targets.append(r)
    else:
        parser.print_help()
        sys.exit(0)


    results = []
    for file_path in targets:
        verbose = not args.json
        res = score_file(file_path, strip_frontmatter=not args.raw, verbose=verbose)
        results.append(res)
        if not args.json:
            print_report(os.path.basename(file_path), res)

    if args.json:
        if len(results) == 1:
            print(json.dumps(results[0], ensure_ascii=False, indent=2))
        else:
            print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
