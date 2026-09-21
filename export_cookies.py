#!/usr/bin/env python3
"""
export_cookies.py
=================
ローカルで認証済みの NotebookLM Cookie (cookies.json) を読み出し、
GitHub Actions の Secret (NOTEBOOKLM_COOKIES) に貼り付け可能な形式で出力します。

使用方法:
  python export_cookies.py
"""

import os
import sys
import json

def main():
    home = os.path.expanduser("~")
    cookie_path = os.path.join(home, ".notebooklm-mcp-cli", "profiles", "default", "cookies.json")

    if not os.path.exists(cookie_path):
        print(f"❌ cookies.json が見つかりません: {cookie_path}", file=sys.stderr)
        print("先に `nlm login` でログインを完了してください。", file=sys.stderr)
        sys.exit(1)

    try:
        with open(cookie_path, "r", encoding="utf-8") as f:
            data = f.read().strip()
        
        # JSON 妥当性チェック
        json.loads(data)
        
        print("\n" + "=" * 60)
        print(" 🔑 NotebookLM Cookie (GitHub Secret: NOTEBOOKLM_COOKIES 用)")
        print("=" * 60)
        print("以下の内容をコピーして、GitHub リポジトリの Secrets に登録してください：")
        print("リポジトリ: Settings -> Secrets and variables -> Actions -> New repository secret")
        print("Name: NOTEBOOKLM_COOKIES")
        print("Secret: (以下のJSON文字列全体)")
        print("-" * 60)
        print(data)
        print("-" * 60)
        
        # Windows クリップボードへのコピー試行
        try:
            import subprocess
            proc = subprocess.Popen(['clip'], stdin=subprocess.PIPE, shell=True)
            proc.communicate(input=data.encode('utf-8'))
            print("📋 クリップボードに自動コピーしました！そのまま貼り付けできます。")
        except Exception:
            pass
            
    except Exception as e:
        print(f"❌ Cookieの読み込みに失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
