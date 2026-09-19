import os
import json
from dotenv import load_dotenv
from typesafe_sdk import TypeSafeClient, Choice, Score, Noul

# .env.local から環境変数を読み込み
load_dotenv(".env.local")

api_key = os.getenv("TYPESAFE_API_KEY")
if not api_key:
    raise ValueError("TYPESAFE_API_KEY が設定されていません。.env.local を確認してください。")

print("TypeSafe Client を初期化中...")
client = TypeSafeClient(api_key=api_key)

# 評価対象のテキスト（State）
sample_ticket = (
    "お世話になっております。昨日申し込んだプランの請求金額が二重に決済されているようです。"
    "早急に確認と返金の手続きをお願いできますでしょうか。業務に支障が出ており大変困っております。"
)

print("\n--- 評価対象の State ---")
print(sample_ticket)
print("------------------------\n")

print("Jev (System One) にリクエスト送信中...")

response = client.system_one(
    state={"ticket": sample_ticket},
    questions={
        # 1. 部署振り分け (Choice)
        "department": Choice(
            instructions="この問い合わせに対応すべき最適な担当部署はどれですか？",
            criteria={
                "billing": "請求、決済、返金、領収書などのお支払い関連",
                "technical_support": "機能のバグ、エラー、接続不具合などの技術サポート",
                "sales": "新規導入、料金プランの相談、契約交渉",
                "general": "その他一般的な問い合わせ",
            },
        ),
        # 2. 感情・フラストレーション度 (Score)
        "frustration_level": Score(
            instructions="顧客の困り度・不満の度合いを評価してください",
            criteria=[
                "穏やか・事実のみを述べている",
                "困惑・やや困っているが冷静",
                "強い不満・業務に支障が出ており焦りや憤りがある",
                "極度の怒り・攻撃的な言動",
            ],
        ),
        # 3. 緊急対応が必要か (Noul: Yes/No 確率)
        "is_urgent": Noul(
            instructions="このメッセージは至急・優先的な即時対応を求めていますか？"
        ),
    },
)

print("\n=== 評価結果 (Jev Response) ===")

# Choice の結果
dept = response.choices["department"]
print(f"\n[Choice: 担当部署]")
print(f"  選択結果: {dept.choice}")
print(f"  確信度 (Confidence): {dept.confidence:.3f}")
print(f"  各選択肢の確率分布:")
for opt, prob in dept.probabilities.items():
    print(f"    - {opt}: {prob:.1%}")

# Score の結果
frust = response.scores["frustration_level"]
print(f"\n[Score: フラストレーション度]")
print(f"  スコア値: {frust.score:.3f}")
print(f"  確信度 (Confidence): {frust.confidence:.3f}")
print(f"  各レベルの確率分布:")
for level, prob in frust.probabilities.items():
    print(f"    - レベル {level}: {prob:.1%}")

# Noul の結果
urgent = response.nouls["is_urgent"]
print(f"\n[Noul: 緊急対応の必要性 (Yes確率)]")
print(f"  確率 (noul): {urgent.noul:.1%}")

# トークン利用量
if hasattr(response, "usage") and response.usage:
    print(f"\n[Usage]")
    print(f"  Input Tokens : {response.usage.input_tokens}")
    print(f"  Output Tokens: {response.usage.output_tokens}")

print("\n実行完了！")
