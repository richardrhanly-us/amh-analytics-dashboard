@echo off
cd /d C:\amh_analytics

echo ========================================
echo AMH update started %date% %time%
echo ========================================

echo Running pipeline...
python -m scripts.run_pipeline
if errorlevel 1 (
    echo Pipeline failed.
    goto end
)

echo Adding app + gitignore + processed files...
git add .gitignore
git add src\app.py
git add src\data_loader.py
git add scripts\run_pipeline.py
git add scripts\parse_checkins.py
git add scripts\parse_rejects.py
git add data\processed\checkins_clean.csv
git add data\processed\rejects_clean.csv
git add data\processed\checkins_history.csv
git add data\processed\rejects_history.csv
git add data\processed\pipeline_status.json

echo Checking for staged changes...
git diff --cached --quiet
if %errorlevel%==0 (
    echo No changes to commit.
    goto pull_only
)

echo Committing changes...
git commit -m "AMH refresh and app updates"

:pull_only
echo Pulling latest repo...
git pull --rebase origin main
if errorlevel 1 (
    echo Git pull/rebase failed.
    goto end
)

echo Pushing to GitHub...
git push origin main
if errorlevel 1 (
    echo Git push failed.
    goto end
)

:end
echo ========================================
echo AMH update finished %date% %time%
echo ========================================
pause
