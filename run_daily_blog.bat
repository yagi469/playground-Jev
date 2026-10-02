@echo off
chcp 65001 > nul
set LOGFILE=C:\Users\user\Dev\playground-Jev\daily_blog_runner.log
echo ======================================== >> "%LOGFILE%"
echo [%date% %time%] 毎朝のブログ自動生成を開始します... >> "%LOGFILE%"

:: 1. playground-Jev で記事生成
cd /d C:\Users\user\Dev\playground-Jev
call .venv\Scripts\python.exe -u daily_paper_blogger.py --queue >> "%LOGFILE%" 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] ❌ 記事生成パイプラインでエラーが発生しました (コード: %errorlevel%) >> "%LOGFILE%"
    exit /b %errorlevel%
)

:: 2. yagibrary にコミット＆プッシュ
cd /d C:\Users\user\Dev\yagibrary
git add src/content/posts docs/book_queue.json >> "%LOGFILE%" 2>&1
git diff --cached --quiet
if %errorlevel% neq 0 (
    git commit -m "post: auto-generate blog article for %date:~0,4%-%date:~5,2%-%date:~8,2%" >> "%LOGFILE%" 2>&1
    git push origin master >> "%LOGFILE%" 2>&1
    echo [%date% %time%] ✅ 記事のプッシュが完了しました。 >> "%LOGFILE%"
) else (
    echo [%date% %time%] ℹ️ 新規記事の生成はありませんでした。 >> "%LOGFILE%"
)

echo [%date% %time%] タスクが正常に終了しました。 >> "%LOGFILE%"
