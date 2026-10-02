$ErrorActionPreference = "Stop"
$LogFile = "C:\Users\user\Dev\playground-Jev\daily_blog_runner.log"

function Write-Log($msg) {
    $now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$now] $msg" | Out-File -FilePath $LogFile -Append -Encoding utf8
    Write-Host "[$now] $msg"
}

Write-Log "========================================"
Write-Log "毎朝のブログ自動生成を開始します..."

try {
    # 1. playground-Jev で記事生成
    Set-Location "C:\Users\user\Dev\playground-Jev"
    Write-Log "記事生成パイプラインを実行中..."
    & "C:\Users\user\Dev\playground-Jev\.venv\Scripts\python.exe" -u daily_paper_blogger.py --queue *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        throw "記事生成パイプラインでエラーが発生しました (コード: $LASTEXITCODE)"
    }

    # 2. yagibrary にコミット＆プッシュ
    Set-Location "C:\Users\user\Dev\yagibrary"
    git add src/content/posts docs/book_queue.json *>> $LogFile

    $status = git status --porcelain src/content/posts docs/book_queue.json
    if ($status) {
        $today = Get-Date -Format "yyyy-MM-dd"
        git commit -m "post: auto-generate blog article for $today" *>> $LogFile
        git push origin master *>> $LogFile
        Write-Log "✅ 記事のプッシュが完了しました。"
    } else {
        Write-Log "ℹ️ 新規記事の生成はありませんでした。"
    }

    Write-Log "タスクが正常に終了しました。"
} catch {
    Write-Log "❌ エラー: $_"
    exit 1
}
