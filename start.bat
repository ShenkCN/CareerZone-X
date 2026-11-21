@echo off
REM 双击入口：把执行策略限制绕开，只对本次进程生效，不修改系统全局策略。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
