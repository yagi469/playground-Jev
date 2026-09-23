"""
config.py
=========
環境設定、外部APIクライアント（Gemini, TypeSafe Jev）、共通定数パスの管理
"""

import os
from dotenv import load_dotenv
from typesafe_sdk import TypeSafeClient

# 環境変数の読み込み
load_dotenv(".env.local")
load_dotenv(".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY")

# yagibrary の posts ディレクトリ（デフォルト保存先）
DEFAULT_YAGIBRARY_POSTS_DIR = os.getenv(
    "YAGIBRARY_POSTS_DIR",
    os.path.normpath(os.path.join(os.path.dirname(__file__), "../yagibrary/src/content/posts"))
)

# yagibrary の static 画像ディレクトリ
DEFAULT_YAGIBRARY_STATIC_DIR = os.getenv(
    "YAGIBRARY_STATIC_DIR",
    os.path.normpath(os.path.join(os.path.dirname(__file__), "../yagibrary/public/images/posts"))
)

# 書籍キューファイルのパス
DEFAULT_BOOK_QUEUE_PATH = os.getenv(
    "BOOK_QUEUE_PATH",
    os.path.normpath(os.path.join(os.path.dirname(__file__), "../yagibrary/docs/book_queue.json"))
)

# Gemini API クライアント（遅延初期化シングルトン）
gemini_client = None

def init_gemini_client():
    """Gemini API クライアントのシングルトン初期化"""
    global gemini_client
    if gemini_client is None:
        try:
            from google import genai
            gemini_client = genai.Client()
        except Exception as e:
            raise RuntimeError(f"Gemini Client 初期化エラー: {e}")
    return gemini_client

# 初期化を試みる（APIキー未設定時は実行時に遅延初期化）
try:
    init_gemini_client()
except Exception:
    pass

# TypeSafe Client
typesafe_client = None
if TYPESAFE_API_KEY:
    try:
        typesafe_client = TypeSafeClient(api_key=TYPESAFE_API_KEY)
    except Exception:
        typesafe_client = None

def get_typesafe_client():
    """TypeSafeClient を安全に取得"""
    global typesafe_client
    if typesafe_client is None:
        key = os.getenv("TYPESAFE_API_KEY")
        if not key:
            raise ValueError("TYPESAFE_API_KEY が設定されていません。.env.local を確認してください。")
        typesafe_client = TypeSafeClient(api_key=key)
    return typesafe_client

# 後方互換性エイリアス
client = gemini_client
jev_client = typesafe_client
