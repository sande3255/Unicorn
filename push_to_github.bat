@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   Push gemini-assistant to GitHub
echo ============================================
echo.
echo Before running this, make sure you've created an EMPTY repo on GitHub:
echo   1. Go to https://github.com/new
echo   2. Give it a name (e.g. gemini-assistant)
echo   3. Do NOT check "Add a README" or any other init options
echo   4. Click "Create repository"
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo ERROR: git isn't installed, or isn't on your PATH.
    echo Install it from https://git-scm.com/download/win and try again.
    echo.
    pause
    exit /b 1
)

set /p REPO="Enter your GitHub repo (e.g. yourusername/gemini-assistant): "
if "%REPO%"=="" (
    echo No repo entered — nothing to do.
    pause
    exit /b 1
)

set REPO_URL=https://github.com/%REPO%.git

if not exist ".git" (
    echo.
    echo Initializing git repo...
    git init
    git branch -M main
)

echo.
echo Staging files...
git add .

git diff --cached --quiet
if not errorlevel 1 (
    echo Nothing new to commit — files already match the last commit.
) else (
    echo Committing...
    git commit -m "gemini-assistant"
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
    echo.
    echo Adding remote: %REPO_URL%
    git remote add origin "%REPO_URL%"
) else (
    echo.
    echo Updating remote to: %REPO_URL%
    git remote set-url origin "%REPO_URL%"
)

echo.
echo Pushing to GitHub — a browser window may open asking you to sign in...
git push -u origin main

if errorlevel 1 (
    echo.
    echo Something went wrong with the push — scroll up to see the error from git.
    echo Common causes: the repo name was typed wrong, the repo isn't actually empty,
    echo or you're not signed into the right GitHub account.
) else (
    echo.
    echo ============================================
    echo   Done! Your code is now at:
    echo   https://github.com/%REPO%
    echo ============================================
)

echo.
pause
