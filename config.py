"""
config.py
=========
環境設定、外部APIクライアント（Gemini, TypeSafe Jev）、共通定数パスの管理
"""

import os
import asyncio
from typing import Optional, Any
from datetime import timezone, timedelta
from dotenv import load_dotenv
from typesafe_sdk import TypeSafeClient

# 日本標準時 (JST)
JST = timezone(timedelta(hours=9))

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

# Antigravity SDK 利用フラグ（デフォルト: 有効）
USE_ANTIGRAVITY_SDK = os.getenv("USE_ANTIGRAVITY_SDK", "true").lower() in ("true", "1", "yes")

# Gemini API クライアント（遅延初期化シングルトン）
gemini_client = None

class AntigravityResponseCompat:
    """google-genai のレスポンス互換オブジェクト"""
    def __init__(self, text: str):
        self._text = text

    @property
    def text(self) -> str:
        return self._text

    @property
    def candidates(self):
        class Part:
            def __init__(self, t):
                self.text = t
        class Content:
            def __init__(self, t):
                self.parts = [Part(t)]
        class Candidate:
            def __init__(self, t):
                self.content = Content(t)
        return [Candidate(self._text)]


class AntigravityModelsAdapter:
    """client.models の互換アダプター"""
    def __init__(self, fallback_client):
        self.fallback_client = fallback_client

    def _run_coro_sync(self, coro):
        import asyncio
        import concurrent.futures
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()

    def _convert_content_item(self, item: Any, agy_types: Any) -> Any:
        """genai の Part や画像・テキストを Antigravity SDK 互換オブジェクトに変換"""
        if isinstance(item, str):
            return item

        # 1. genai types.Part (inline_data)
        if hasattr(item, "inline_data") and getattr(item, "inline_data", None) is not None:
            data = item.inline_data.data
            mime = item.inline_data.mime_type or "application/octet-stream"
            return agy_types.from_bytes(data=data, mime_type=mime)

        # 2. PIL Image オブジェクト
        try:
            from PIL import Image
            if isinstance(item, Image.Image):
                import io
                buf = io.BytesIO()
                fmt = item.format or "PNG"
                item.save(buf, format=fmt)
                return agy_types.from_bytes(data=buf.getvalue(), mime_type=f"image/{fmt.lower()}")
        except ImportError:
            pass

        # 3. 生の bytes オブジェクト（PDFなどと推測される場合）
        if isinstance(item, (bytes, bytearray)):
            # デフォルトは PDF または octet-stream
            return agy_types.from_bytes(data=bytes(item), mime_type="application/pdf")

        # 4. text 属性を持つオブジェクト
        if hasattr(item, "text") and isinstance(item.text, str):
            return item.text

        return str(item)

    async def _call_agent(self, prompt_input: Any, system_instruction: Optional[str] = None) -> str:
        from google.antigravity import Agent, LocalAgentConfig
        config = LocalAgentConfig(
            system_instructions=system_instruction,
        )
        async with Agent(config) as agent:
            resp = await agent.chat(prompt_input)
            if asyncio.iscoroutinefunction(resp.text):
                return await resp.text()
            res = resp.text()
            if asyncio.iscoroutine(res):
                return await res
            return str(res)

    def generate_content(self, model: str, contents: Any, config: Any = None, **kwargs):
        # system_instruction の抽出
        sys_inst = None
        if config is not None:
            if hasattr(config, "system_instruction"):
                sys_inst = config.system_instruction
            elif isinstance(config, dict):
                sys_inst = config.get("system_instruction")

        if isinstance(sys_inst, (list, tuple)):
            sys_inst = "\n".join(str(s) for s in sys_inst)
        elif sys_inst is not None:
            sys_inst = str(sys_inst)

        # Antigravity SDK 経由での生成を試行（マルチモーダル対応）
        try:
            from google.antigravity import types as agy_types
            if isinstance(contents, (list, tuple)):
                converted_contents = [self._convert_content_item(c, agy_types) for c in contents]
            else:
                converted_contents = self._convert_content_item(contents, agy_types)

            res_text = self._run_coro_sync(self._call_agent(converted_contents, system_instruction=sys_inst))
            return AntigravityResponseCompat(res_text)
        except Exception as e:
            print(f"⚠️ Antigravity SDK 生成エラー: {e} -> 従来の genai にフォールバックします")
            return self.fallback_client.models.generate_content(
                model=model, contents=contents, config=config, **kwargs
            )


class AntigravityClientAdapter:
    """Antigravity SDK を優先利用する genai.Client 互換ラッパー"""
    def __init__(self, raw_client):
        self.raw_client = raw_client
        self.models = AntigravityModelsAdapter(raw_client)

    def __getattr__(self, name):
        return getattr(self.raw_client, name)


def init_gemini_client():
    """Gemini API クライアントのシングルトン初期化（Antigravity SDK 優先）"""
    global gemini_client
    if gemini_client is None:
        raw_client = None
        try:
            from google import genai
            raw_client = genai.Client()
        except Exception as e:
            print(f"⚠️ genai.Client 初期化警告: {e}")

        if USE_ANTIGRAVITY_SDK:
            try:
                import google.antigravity
                gemini_client = AntigravityClientAdapter(raw_client)
            except ImportError:
                print("ℹ️ google-antigravity が見つからないため、通常の genai.Client を使用します")
                gemini_client = raw_client
        else:
            gemini_client = raw_client

        if gemini_client is None:
            raise RuntimeError("クライアントの初期化に失敗しました。")

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
