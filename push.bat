@echo off
cd /d "%~dp0"
git add -A
git status --short
git commit -m "update %date% %time%"
git push
