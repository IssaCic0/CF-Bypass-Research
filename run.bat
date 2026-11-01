@echo off
REM CF-Bypass-Research 统一入口（Windows 命令行版，等价于 run.ps1）
REM 用法： run.bat test | go | verify | doctor | smoke | bench | start | stop
setlocal
set "SCRIPT=%~dp0run.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
exit /b %ERRORLEVEL%
