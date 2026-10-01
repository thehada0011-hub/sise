@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -X utf8 collector.py %*
if "%~1"=="" pause
