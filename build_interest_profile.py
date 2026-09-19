import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# Google Driveから検出された代表的なarXiv ID
ARXIV_IDS = [
    "2609.19252",
    "2609.15929",
    "2609.16136",
    "2609.13381",
    "2609.12088",
    "2609.09007",
    "2609.07826",
    "2006.13892",
    "1312.5344",
]

CLASSICS_IN_DRIVE = [
    "Steven Weinberg: The Quantum Theory of Fields (Vol 1, 2, 3) - 場の量子論、対称性の自発的破れ、超対称性",
    "hep-th/8809005 (Witten or early topological/conformal field theory)",
    "4d SCFT and 2d chiral algebra (Beem et al. 2013 / Infinite Chiral Symmetry in Four Dimensions)",
    "橋本幸士先生の『Dブレーン 超弦理論を正しく学ぶための最前線』",
]

def fetch_arxiv_details(arxiv_ids):
    id_list_str = ",".join(arxiv_ids)
    url = f"http://export.arxiv.org/api/query?id_list={id_list_str}&max_results=20"
    req = urllib.request.Request(url, headers={"User-Agent": "PlaygroundJev/1.0"})
    papers = []
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read().decode("utf-8")
            root = ET.fromstring(content)
            atom_ns = "{http://www.w3.org/2005/Atom}"
            for entry in root.findall(f"{atom_ns}entry"):
                title = entry.find(f"{atom_ns}title").text.strip().replace("\n", " ")
                summary = entry.find(f"{atom_ns}summary").text.strip().replace("\n", " ")
                paper_id = entry.find(f"{atom_ns}id").text.strip().split("/abs/")[-1]
                papers.append({
                    "id": paper_id,
                    "title": title,
                    "summary": summary[:400]
                })
    except Exception as e:
        print(f"Failed to fetch arxiv: {e}")
    return papers

def generate_profile():
    print("Fetching arXiv details for files found in Google Drive...")
    papers = fetch_arxiv_details(ARXIV_IDS)
    print(f"Fetched {len(papers)} papers metadata.")

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    prompt = f"""
あなたは理論物理学（素粒子論・場の量子論・量子情報・数理物理）に精通したAIアナリストです。
以下は、ユーザーのGoogle Driveから検出された所蔵書籍および最近ダウンロード・精読しているarXiv論文の一覧です。

【所蔵書籍・テキスト】
{json.dumps(CLASSICS_IN_DRIVE, ensure_ascii=False, indent=2)}

【最近ダウンロード・精読中のarXiv論文】
{json.dumps(papers, ensure_ascii=False, indent=2)}

これらの情報から、ユーザーの研究関心・物理的興味の対象プロファイルを分析し、
arXiv論文選定（hep-th, quant-ph）に直接活用できるJSON形式で出力してください。

出力フォーマット（JSON厳密準拠）:
{{
  "core_themes": ["主要テーマ1", "主要テーマ2", ...],
  "keywords": ["キーワード1", "キーワード2", ...],
  "preferred_approaches": ["理論的・数理的アプローチの特徴", ...],
  "target_categories": ["hep-th", "quant-ph", "math-ph", ...],
  "evaluation_criteria_for_jev": "Jevが論文のアブストラクトを評価する際に、どのような観点を加点・重視すべきかの指示プロンプト文"
}}
"""
    print("Analyzing user interests with Gemini 3.8...")
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )

    profile_data = json.loads(response.text)
    profile_data["source_books"] = CLASSICS_IN_DRIVE
    profile_data["source_sample_papers"] = [p["title"] for p in papers]

    output_path = os.path.join(os.path.dirname(__file__), "user_interests.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(profile_data, f, ensure_ascii=False, indent=2)

    print(f"User profile successfully generated and saved to {output_path}!")
    print(json.dumps(profile_data, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    generate_profile()
