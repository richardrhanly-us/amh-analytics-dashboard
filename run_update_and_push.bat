@echo off
cd /d C:\amh_analytics

echo ==============================
echo AMH update started
echo ==============================

git pull --rebase origin main
if errorlevel 1 goto :error

python -m scripts.run_pipeline
if errorlevel 1 goto :error

git add data\processed\checkins_clean.csv
git add data\processed\checkins_history.csv
git add data\processed\rejects_clean.csv
git add data\processed\rejects_history.csv
git add data\processed\pipeline_status.json

git diff --cached --quiet
if %errorlevel%==0 goto :nochanges

git commit -m "Auto-update AMH processed data"
if errorlevel 1 goto :error

git push origin main
if errorlevel 1 goto :error

echo.
echo Update complete and pushed to GitHub.
goto :end

:nochanges
echo.
echo No data changes detected. Nothing to commit.
goto :end

:error
echo.
echo Update failed.
goto :end

:end