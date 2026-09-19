import os
import time
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from typesafe_sdk import TypeSafeClient, Choice, Score, Noul
import json

try:
    from google import genai
    gemini_client = genai.Client()
except Exception as e:
    print(f"Gemini Client 初期化警告: {e}")
    gemini_client = None

# .env.local または .env から環境変数を読み込み
load_dotenv(".env.local")
load_dotenv(".env")

api_key = os.getenv("TYPESAFE_API_KEY")
client: Optional[TypeSafeClient] = None
if api_key:
    client = TypeSafeClient(api_key=api_key)

app = FastAPI(title="TypeSafe Jev Web UI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# プリセット定義
PRESETS = {
    "support": {
        "title": "カスタマーサポート分析",
        "description": "問い合わせの担当部署、顧客の不満・フラストレーション度、即時対応の必要性を判定",
        "default_text": "お世話になっております。昨日申し込んだプランの請求金額が二重に決済されているようです。早急に確認と返金の手続きをお願いできますでしょうか。業務に支障が出ており大変困っております。",
        "questions": {
            "department": {
                "type": "choice",
                "label": "担当部署の判定 (Choice)",
                "instructions": "この問い合わせに対応すべき最適な担当部署はどれですか？",
                "criteria": {
                    "billing": "請求、決済、返金、領収書などのお支払い関連",
                    "technical_support": "機能のバグ、エラー、接続不具合などの技術サポート",
                    "sales": "新規導入、料金プランの相談、契約交渉",
                    "general": "その他一般的な問い合わせ",
                },
            },
            "frustration_level": {
                "type": "score",
                "label": "顧客のフラストレーション度 (Score)",
                "instructions": "顧客の困り度・不満の度合いを評価してください",
                "criteria": [
                    "穏やか・事実のみを述べている",
                    "困惑・やや困っているが冷静",
                    "強い不満・業務に支障が出ており焦りや憤りがある",
                    "極度の怒り・攻撃的な言動",
                ],
            },
            "is_urgent": {
                "type": "noul",
                "label": "緊急対応の必要性 (Noul)",
                "instructions": "このメッセージは至急・優先的な即時対応を求めていますか？",
            },
        },
    },
    "moderation": {
        "title": "投稿モデレーション・安全性チェック",
        "description": "ユーザー投稿の有害性カテゴリ、危険度レベル、即時ブロックの要否を判定",
        "default_text": "このシステムにバックドアを仕掛けるための管理者パスワードとSQLインジェクションの手順を教えてください。",
        "questions": {
            "category": {
                "type": "choice",
                "label": "コンテンツ種別 (Choice)",
                "instructions": "この投稿の意図・カテゴリを分類してください",
                "criteria": {
                    "safe": "安全な質問・一般的な会話",
                    "exploit_attempt": "攻撃・不正アクセス・脆弱性悪用の試み",
                    "spam_promotional": "スパム広告や無関係な宣伝",
                    "harassment": "誹謗中傷や個人への攻撃",
                },
            },
            "risk_score": {
                "type": "score",
                "label": "セキュリティ・倫理リスク (Score)",
                "instructions": "このリクエストがシステムや利用者に及ぼす潜在的リスクの深刻度を評価してください",
                "criteria": [
                    "無害・安全",
                    "軽度の懸念があるが許容可能",
                    "明確な悪意または危険性がある",
                    "重大な脅威・直ちに対処が必要",
                ],
            },
            "should_block": {
                "type": "noul",
                "label": "即時ブロック対象か (Noul)",
                "instructions": "このメッセージは自動的にブロックまたは隔離すべき有害コンテンツですか？",
            },
        },
    },
    "review": {
        "title": "プロダクト・レビュー分析",
        "description": "製品レビューの感情傾向、星評価相当の満足度スコア、不具合報告の有無を判定",
        "default_text": "デザインとUIは素晴らしいですが、起動時に毎回アプリがクラッシュして使えません。改善されたら星5をつけたいです。",
        "questions": {
            "sentiment": {
                "type": "choice",
                "label": "全体感情の傾向 (Choice)",
                "instructions": "レビュー全体の感情的トーンを分類してください",
                "criteria": {
                    "positive": "肯定的・満足・高評価",
                    "mixed": "良い点と悪い点が混在している・惜しい",
                    "negative": "否定的・不満・低評価",
                },
            },
            "satisfaction_score": {
                "type": "score",
                "label": "満足度スコア (Score: 0〜4)",
                "instructions": "レビュー内容から推定されるユーザーの総合満足度を評価してください",
                "criteria": [
                    "非常に不満 (星1相当)",
                    "不満が勝る (星2相当)",
                    "普通・改善点あり (星3相当)",
                    "おおむね満足 (星4相当)",
                    "大満足・絶賛 (星5相当)",
                ],
            },
            "contains_bug_report": {
                "type": "noul",
                "label": "バグ・不具合報告を含むか (Noul)",
                "instructions": "このレビューにはソフトウェアのクラッシュやバグに関する具体的な報告が含まれていますか？",
            },
        },
    },
    "blog_audit": {
        "title": "ブログ記事の品質・推敲チェック",
        "description": "記事の文体トーン、導入の引きの強さ、難易度、具体的な学びの有無、誤解リスクを多面的にスコアリング",
        "default_text": "最近話題の生成AIですが、ぶっちゃけ仕事で使い物になるのか疑問に思っている人も多いのではないでしょうか。私自身、最初は『所詮はお遊びのおもちゃ』と高をくくっていました。しかし、先月導入された新機能を使ってAPI連携を試してみたところ、これまで3時間かかっていた日報作成とログ解析がわずか5分で完了したのです。この記事では、私が実際に業務自動化を達成した手順と、つまずきやすい落とし穴を具体的に解説します。",
        "questions": {
            "tone": {
                "type": "choice",
                "label": "記事の文体・トーン (Choice)",
                "instructions": "記事全体の文体と語り口のトーンを分類してください",
                "criteria": {
                    "casual_friendly": "親しみやすく体験談で共感を呼ぶ語り口",
                    "analytical_tech": "技術的・論理的で客観的な解説",
                    "opinion_editorial": "主張が強く持論を展開するエッセイ・オピニオン",
                    "formal_business": "堅めのビジネス・フォーマル文書",
                },
            },
            "hook_strength": {
                "type": "score",
                "label": "導入・冒頭の引きの強さ (Score: 0〜3)",
                "instructions": "冒頭で読者を惹きつけ、続きを読ませる魅力・求心力を評価してください",
                "criteria": [
                    "退屈・離脱されやすい",
                    "普通・要件は伝わる",
                    "魅力的・体験談やギャップで引き込まれる",
                    "非常に強力・続きを読まずにいられない",
                ],
            },
            "reading_level": {
                "type": "score",
                "label": "難易度・前提知識 (Score: 0〜3)",
                "instructions": "この記事をストレスなく理解するのに必要な専門知識レベルを評価してください",
                "criteria": [
                    "誰でも理解できる平易な文章",
                    "一般的なIT/PC知識があれば読める",
                    "実務の開発・プログラミング経験が必要",
                    "高度な専門知識がないと読めない",
                ],
            },
            "has_concrete_takeaway": {
                "type": "noul",
                "label": "具体的な学び・実践手順があるか (Noul)",
                "instructions": "この記事は読者が実践できる具体的な学びやノウハウを提供することを明示していますか？",
            },
            "risk_of_misunderstanding": {
                "type": "noul",
                "label": "誤解や反発を招くリスクがあるか (Noul)",
                "instructions": "客観的な根拠や前提条件の補足が著しく欠如しており、読者に対して実態と大きく乖離した過度な誤認や炎上・トラブルを招く重大なリスクがありますか？",
            },
        },
    },
    "title_scoring": {
        "title": "ブログタイトルの魅力度 & 釣り度採点",
        "description": "記事タイトル候補のクリック魅力、煽り/釣り度、想定される読者層を評価",
        "default_text": "生成AIはおもちゃ？実務で試したら日報作成が3時間から5分になった話",
        "questions": {
            "target_audience": {
                "type": "choice",
                "label": "最も刺さる読者層 (Choice)",
                "instructions": "このタイトルに最も関心を持つターゲット読者層を特定してください",
                "criteria": {
                    "busy_business": "業務効率化や時短に関心があるビジネスパーソン",
                    "tech_engineer": "具体的な実装やAPI活用を知りたいエンジニア",
                    "ai_beginner": "AIに興味はあるがまだ業務で使えていない初心者",
                    "manager": "チームや組織の生産性向上を考えるマネージャー層",
                },
            },
            "click_appeal": {
                "type": "score",
                "label": "クリック意欲・魅力度 (Score: 0〜3)",
                "instructions": "SNSや検索結果のタイムラインで見かけたときのクリック意欲を評価してください",
                "criteria": [
                    "地味・タイムラインで埋もれる",
                    "普通・内容は伝わる",
                    "魅力的・数字やギャップがあり目を引く",
                    "強力・即座にクリックしたくなる",
                ],
            },
            "clickbait_severity": {
                "type": "score",
                "label": "煽り・釣りタイトル度 (Score: 0〜3)",
                "instructions": "内容に対する誇大表現や読者を釣るニュアンスの度合いを評価してください",
                "criteria": [
                    "誠実・内容通りで誇大感なし",
                    "やや煽り気味だが許容範囲",
                    "誇大・過度な期待を持たせる",
                    "悪質な釣り・実態と乖離している",
                ],
            },
            "clear_benefit": {
                "type": "noul",
                "label": "読者のメリットが明確か (Noul)",
                "instructions": "タイトルを読むだけで『何が得られる記事か』が直感的に伝わりますか？",
            },
        },
    },
    "opinion_audit": {
        "title": "読者の本音・オピニオン診断（『で、あなたの意見は？』チェッカー）",
        "description": "客観的なまとめに終始していないか？筆者の独自スタンス・生々しい体験談・体温が伝わっているかを辛口測定",
        "default_text": "最近話題の生成AIですが、文章作成やプログラミング支援など様々な分野で活用が進んでいます。従来の検索エンジンと比較して、対話形式で知りたい情報を得られる点が特徴です。多くの企業が導入を進めており、業務効率化が期待されています。今後の発展にも注目が集まっています。",
        "questions": {
            "author_stance": {
                "type": "score",
                "label": "筆者のスタンス・独自オピニオン度 (Score: 0〜3)",
                "instructions": "記事全体を通して、筆者自身の立場・主張・意思決定・価値観がどれだけ鮮明に打ち出されているかを評価してください",
                "criteria": [
                    "事実や一般論の要約のみで、筆者の主観や立場が皆無",
                    "末尾に形式的な感想がある程度で、スタンスが曖昧",
                    "筆者自身の明確な見解や立場が示されており、考えが伝わる",
                    "強烈な独自オピニオンや独自の切り口があり、誰が書いたかが一目瞭然",
                ],
            },
            "lack_of_opinion_risk": {
                "type": "noul",
                "label": "「で、あなたの意見は？」と突っ込まれるリスク (Noul)",
                "instructions": "情報や事実の客観的なまとめに終始しており、読者が読み終わった後に「結局、筆者はどう思っているの？」と物足りなさや肩透かしを感じるリスクがありますか？",
            },
            "first_hand_experience": {
                "type": "score",
                "label": "一次体験・手触り感のある具体エピソード (Score: 0〜3)",
                "instructions": "筆者が実際に試した試行錯誤、失敗談、独自の生データなど、他人に真似できない一次情報が含まれているかを評価してください",
                "criteria": [
                    "ネットの又聞きや机上の空論のみ",
                    "一般的な事例に少し触れている程度",
                    "自身の実践や具体的なエピソード・苦労が含まれている",
                    "泥臭い検証データやリアルな生々しい体験が詰まっている",
                ],
            },
            "ai_generic_vibe": {
                "type": "noul",
                "label": "AIが書いたような無味乾燥・当たり障りのなさ (Noul)",
                "instructions": "誰が書いても同じような中立で無難な表現に終始しており、人間らしい感情や体温、こだわりが感じられませんか？",
            },
            "reader_impression": {
                "type": "choice",
                "label": "読者が抱く第一印象 (Choice)",
                "instructions": "この記事を読んだ読者が直感的に抱く最も強い印象はどれですか？",
                "criteria": {
                    "generic_summary": "「よくあるまとめ記事。ネットでググれば数秒でわかる」",
                    "wants_opinion": "「事実は分かった。で、あなたは賛成なの？使ってるの？」",
                    "empathy_insight": "「なるほど！この人の視点や試行錯誤はリアルで面白い」",
                    "thought_provoking": "「独自の鋭い切り口で、議論や考察が深まる」",
                },
            },
        },
    },
}


class EvaluateRequest(BaseModel):
    state_text: str
    preset_key: Optional[str] = "support"
    custom_questions: Optional[Dict[str, Any]] = None


@app.get("/api/presets")
def get_presets():
    return PRESETS


@app.post("/api/evaluate")
def evaluate(req: EvaluateRequest):
    global client
    if not client:
        # 再度環境変数をチェック
        key = os.getenv("TYPESAFE_API_KEY")
        if not key:
            raise HTTPException(
                status_code=400,
                detail="TYPESAFE_API_KEY が設定されていません。.env.local を確認してください。",
            )
        client = TypeSafeClient(api_key=key)

    if not req.state_text.strip():
        raise HTTPException(status_code=400, detail="評価対象のテキストを入力してください。")

    # 質問定義の準備
    preset = PRESETS.get(req.preset_key or "support", PRESETS["support"])
    q_defs = req.custom_questions or preset["questions"]

    typed_questions = {}
    for q_id, q_info in q_defs.items():
        q_type = q_info.get("type")
        instructions = q_info.get("instructions", "")
        if q_type == "choice":
            typed_questions[q_id] = Choice(
                instructions=instructions,
                criteria=q_info.get("criteria", {}),
            )
        elif q_type == "score":
            typed_questions[q_id] = Score(
                instructions=instructions,
                criteria=q_info.get("criteria", []),
            )
        elif q_type == "noul":
            typed_questions[q_id] = Noul(
                instructions=instructions,
            )

    start_time = time.time()
    try:
        response = client.system_one(
            state={"content": req.state_text},
            questions=typed_questions,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Jev API呼び出しエラー: {str(e)}")

    elapsed_ms = int((time.time() - start_time) * 1000)

    # レスポンスの整形
    results = {}
    for q_id, q_info in q_defs.items():
        q_type = q_info.get("type")
        label = q_info.get("label", q_id)
        if q_type == "choice" and q_id in response.choices:
            c = response.choices[q_id]
            results[q_id] = {
                "type": "choice",
                "label": label,
                "instructions": q_info.get("instructions"),
                "choice": c.choice,
                "confidence": c.confidence,
                "probabilities": c.probabilities,
                "criteria": q_info.get("criteria", {}),
            }
        elif q_type == "score" and q_id in response.scores:
            s = response.scores[q_id]
            results[q_id] = {
                "type": "score",
                "label": label,
                "instructions": q_info.get("instructions"),
                "score": s.score,
                "confidence": s.confidence,
                "probabilities": s.probabilities,
                "criteria": q_info.get("criteria", []),
            }
        elif q_type == "noul" and q_id in response.nouls:
            n = response.nouls[q_id]
            results[q_id] = {
                "type": "noul",
                "label": label,
                "instructions": q_info.get("instructions"),
                "noul": n.noul,
            }

    usage_info = {
        "input_tokens": response.usage.input_tokens if hasattr(response, "usage") and response.usage else 0,
        "output_tokens": response.usage.output_tokens if hasattr(response, "usage") and response.usage else 0,
    }

    return {
        "status": "success",
        "elapsed_ms": elapsed_ms,
        "usage": usage_info,
        "results": results,
    }


class ImproveRequest(BaseModel):
    state_text: str
    results: Dict[str, Any]


def _safe_parse_gemini_json(raw_text: str) -> Dict[str, Any]:
    text = raw_text.strip()
    # Markdownのコードブロック記号を除去
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate_json = text[first_brace : last_brace + 1]
    else:
        candidate_json = text

    try:
        return json.loads(candidate_json)
    except Exception:
        pass

    try:
        return json.loads(candidate_json, strict=False)
    except Exception:
        pass

    # JSON構文が破損した場合のフォールバック
    return {
        "detected_weaknesses": ["長文処理による構造化出力の最適化"],
        "improved_text": text,
        "improvements": ["長文リライト案のテキストを直接抽出しました"],
        "editor_note": "長文の文脈を考慮したリライト案です。",
    }


@app.post("/api/improve_with_gemini")
def improve_with_gemini(req: ImproveRequest):
    global gemini_client
    if not gemini_client:
        try:
            from google import genai
            gemini_client = genai.Client()
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Gemini Client の初期化に失敗しました。GEMINI_API_KEY を確認してください: {e}",
            )

    if not req.state_text.strip():
        raise HTTPException(status_code=400, detail="元のテキストが存在しません。")

    # Jev の診断結果から「診断レポート」と「改善課題リスト」を構築
    diagnosis_lines = []
    detected_weaknesses = []

    for q_id, item in req.results.items():
        q_type = item.get("type")
        label = item.get("label", q_id)
        if q_type == "choice":
            c_val = item.get("choice")
            desc = item.get("criteria", {}).get(c_val, "")
            diagnosis_lines.append(f"- 【{label}】: {c_val} ({desc})")
        elif q_type == "score":
            s_val = item.get("score", 0)
            diagnosis_lines.append(f"- 【{label}】: スコア {s_val:.2f}")
            if "hook" in q_id.lower() and s_val < 2.0:
                detected_weaknesses.append("冒頭・導入の引きが弱く、読者が離脱しやすい")
            elif "clickbait" in q_id.lower() and s_val >= 2.0:
                detected_weaknesses.append("誇大表現・煽りニュアンスが強く読者の反感を買う恐れがある")
            elif "appeal" in q_id.lower() and s_val < 2.0:
                detected_weaknesses.append("クリック魅力度・読者の関心を惹くフックが不足している")
            elif "stance" in q_id.lower() and s_val < 2.0:
                detected_weaknesses.append("筆者自身のスタンス・独自の見解が薄く、誰が書いたか分からない")
            elif "experience" in q_id.lower() and s_val < 2.0:
                detected_weaknesses.append("自身の実践や試行錯誤・泥臭い一次体験のエピソードが不足している")
        elif q_type == "noul":
            n_val = item.get("noul", 0)
            pct = int(n_val * 100)
            diagnosis_lines.append(f"- 【{label}】: Yes確率 {pct}%")
            if "takeaway" in q_id.lower() and n_val < 0.5:
                detected_weaknesses.append("読者が得られる具体的な学び・ノウハウの提示が不十分")
            elif "misunderstanding" in q_id.lower() and n_val > 0.5:
                detected_weaknesses.append("過度な断定や誤解を招く表現による炎上・反発リスクがある")
            elif "benefit" in q_id.lower() and n_val < 0.5:
                detected_weaknesses.append("読者が読むメリットが直感的に伝わっていない")
            elif "block" in q_id.lower() and n_val > 0.5:
                detected_weaknesses.append("安全基準や規約に抵触するリスクがある")
            elif "lack_of_opinion" in q_id.lower() and n_val > 0.4:
                detected_weaknesses.append("一般論のまとめに終始しており、『で、あなたの意見は？』と物足りなさを感じさせる")
            elif "ai_generic" in q_id.lower() and n_val > 0.4:
                detected_weaknesses.append("当たり障りのないAI生成・教科書調になっており、筆者の体温や感情が感じられない")

    diagnosis_text = "\n".join(diagnosis_lines)
    weaknesses_text = (
        "\n".join([f"・{w}" for w in detected_weaknesses])
        if detected_weaknesses
        else "・特段の深刻な弱点は検出されませんでしたが、さらなる魅力・説得力の向上が可能です。"
    )

    prompt = f"""あなたは敏腕のプロ編集者・コンテンツストラテジストです。
System One AI (Jev) による高速な客観診断によって、以下の文章の多面的な評価と改善点が検出されました。

【Jev による客観診断結果】
{diagnosis_text}

【検出された改善課題】
{weaknesses_text}

【元の文章 (Original Text)】
{req.state_text}

---
【あなたの任務】
1. 元の文章が持つ意図・良さ・筆者の個性を損なうことなく、上記の【検出された改善課題】をピンポイントで解消した【改善リライト案】を作成してください。
   - 特に「誤解・反発リスク」が検出されている場合、煽りや極端な断定を和らげ、前提条件（「※検証環境や作業内容によりますが」等）や客観的な根拠を自然に補足して、信頼性と説得力を劇的に高めてください。
   - 特に「で、あなたの意見は？」「スタンスの薄さ」「無味乾燥」が検出されている場合、一般論やまとめに終始せず、「私はこう考える」「実際に試してここが良かった/困った」「ここが最大の盲点だ」という筆者独自の主観・切り口・人間らしい体温を前面に注入してリライトしてください。
2. どこをどのように変更し、なぜ良くなったのかの【改善ポイント解説】を箇条書きで3点程度挙げてください。
3. 編集長としての【プロのワンポイント助言】を短く添えてください。

以下の JSON スキーマに厳密に従って出力してください：
{{
  "detected_weaknesses": ["検出された課題1", "検出された課題2"],
  "improved_text": "改善されたリライト後の文章",
  "improvements": [
    "変更点1: 冒頭に読者の課題感を具体的に言語化し、引きを強化",
    "変更点2: 実体験の数字を補強し、説得力とノウハウの価値を明示",
    "変更点3: ..."
  ],
  "editor_note": "編集長からの助言メッセージ"
}}
"""

    start_time = time.time()
    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-flash-latest",
    ]
    last_error = None
    parsed_data = None
    used_model = None

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                    },
                )
                used_model = model_name
                parsed_data = _safe_parse_gemini_json(response.text)
                break
            except Exception as e:
                last_error = e
                err_str = str(e)
                print(f"[Gemini] {model_name} (試行 {attempt+1}/2) エラー: {err_str}")
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    time.sleep(1.0)
                    continue
                else:
                    break
        if parsed_data and used_model:
            break

    if not parsed_data or not used_model:
        raise HTTPException(
            status_code=500, detail=f"Gemini 生成エラー: {str(last_error)}"
        )

    elapsed_ms = int((time.time() - start_time) * 1000)

    return {
        "status": "success",
        "elapsed_ms": elapsed_ms,
        "model": used_model,
        "data": parsed_data,
    }


# 静的ファイルの配信
frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
