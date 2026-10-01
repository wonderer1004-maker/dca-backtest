@echo off
REM Morning auto-trader (32-bit Python 3.8 + Kiwoom OpenAPI+)
cd /d %~dp0
py -3.8-32 trader.py >> log\trader_console.log 2>&1
