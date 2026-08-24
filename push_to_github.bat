@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   Push UNICORN to GitHub
echo ============================================
echo.
echo If you already have a GitHub repo for this (e.g. sande3255/Unicorn),
echo just enter it below. If you're starting fresh, create an EMPTY repo
echo first:
echo   1. Go to https://github.com/new
echo   2. Give it a name (e.g. Unicorn)
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

set /p REPO="Enter your GitHub repo (owner/repo, or you can paste the full github.com link): "
if "%REPO%"=="" (
    echo No repo entered — nothing to do.
    pause
    exit /b 1
)

rem Accept either "owner/repo" or a full URL like https://github.com/owner/repo —
rem strip any protocol/host/.git suffix/trailing slash so both work the same way.
rem (This is the fix for the "https://github.com/https://github.com/..." bug —
rem pasting the full link here used to double up with the URL built below.)
set "REPO=%REPO:https://github.com/=%"
set "REPO=%REPO:http://github.com/=%"
set "REPO=%REPO:github.com/=%"
set "REPO=%REPO:.git=%"
if "%REPO:~-1%"=="/" set "REPO=%REPO:~0,-1%"

echo Using repo: %REPO%
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
    git commit -m "UNICORN update"
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
echo Checking for changes already made directly on GitHub (e.g. via the website)...
git fetch origin main >nul 2>nul
git rev-parse --verify origin/main >nul 2>nul
if not errorlevel 1 (
    rem Merge in anything that's on GitHub but not local. -X ours means: if
    rem a file conflicts, keep the LOCAL version (this folder is the source
    rem of truth) rather than stopping to ask you to resolve it by hand.
    git merge origin/main --no-edit -X ours --allow-unrelated-histories >nul 2>nul
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
