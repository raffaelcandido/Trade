@echo off
chcp 65001 >nul
title B3 Day Trader - Motor Local
cd /d "%~dp0"

echo ==================================================
echo   B3 Day Trader - Iniciando motor local (BTG MT5)
echo ==================================================
echo.
echo [1/2] Abrindo o painel no navegador em 2 segundos...
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765"

echo [2/2] Iniciando servidor em http://127.0.0.1:8765
echo       Deixe esta janela aberta (pode minimizar).
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.12 -m b3_trader web
) else (
    python -m b3_trader web
)

echo.
echo O servidor foi encerrado.
pause
