@echo off
chcp 65001 >nul
title 获客管理系统
echo ================================================
echo   获客管理系统
echo   浏览器会自动打开 http://localhost:5000
echo   使用期间不要关闭这个窗口
echo ================================================
echo.
cd /d "%~dp0"
start "" cmd /c "timeout /t 2 >nul & start http://localhost:5000"
python app.py
pause
