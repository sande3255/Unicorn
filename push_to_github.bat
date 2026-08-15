@echo off
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
    echo Git doesn't seem to be installed ^(or isn't on PATH^).
    echo Install it from https://git-scm.com/download/win and try again.
    pause
    exit /b 1
)

if not exist ".git" (
    echo Initializing a new git repo here...
    git init
    git branch -M main
) else (
    echo Existing git repo found here, reusing it.
)

echo.
echo Paste the HTTPS URL of your GitHub repo below.
echo   e.g. https://github.com/yourname/unicorn-predictmarket.git
echo   ^(Create an empty repo on github.com first if you haven't yet ^-
echo    don't initialize it with a README, so there's no merge conflict.^)
echo.
set /p REPO_URL="Repo URL: "

git remote remove origin >nul 2>nul
git remote add origin "%REPO_URL%"

echo.
echo Staging files (this respects .gitignore, so venv/ and the .db won't be included)...
git add -A

git commit -m "UNICORN prediction market app"
if errorlevel 1 (
    echo.
    echo Nothing new to commit, or commit failed — check the message above.
)

echo.
echo Pushing to %REPO_URL% ...
git push -u origin main

echo.
echo If that succeeded, your code is now on GitHub — head to railway.app
echo and create a new project "Deploy from GitHub repo" pointing at it.
pause
