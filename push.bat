@echo off
cd /d "%~dp0"
git add -A
git commit -m "update %date% %time%"
git push --force