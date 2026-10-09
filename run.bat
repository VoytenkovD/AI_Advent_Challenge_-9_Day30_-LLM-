@echo off
rem Private LLM service. Keep this window open: the service works while it is running.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python "%~dp0app\server.py"
if errorlevel 1 pause
