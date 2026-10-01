@echo off
REM Daily scan after market close (64-bit Python 3.11)
cd /d %~dp0
py -3.11-64 scan.py >> log\scan_console.log 2>&1
