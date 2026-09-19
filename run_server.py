import uvicorn
import os
import sys

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    base_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(base_dir, "backend")
    print(f"\n=======================================================")
    print(f" 🚀 TypeSafe Jev Web UI を起動中...")
    print(f" 👉 ブラウザで開く: http://localhost:{port}")
    print(f"=======================================================\n")
    uvicorn.run(
        "backend.app:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        reload_dirs=[base_dir, backend_dir],
    )
