@echo off
chcp 65001 >nul
cd /d "%~dp0"
title APEX EDGE — Server Locale + Cloudflare Quick Tunnel

echo ================================================================
echo   APEX EDGE — Avvio Server Locale + Archivio Unico + Tunnel
echo ================================================================
echo.

REM 1) Controlla Python 3
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERRORE] Python non trovato nel PATH. Installa Python 3 da python.org.
    pause
    exit /b 1
)

REM 2) Crea .env da .env.example la prima volta
if not exist ".env" (
    echo [INFO] File .env non trovato: lo creo da .env.example...
    copy /Y ".env.example" ".env" >nul
    echo.
    echo ================================================================
    echo   COMPILA IL FILE .env CON TOKEN TELEGRAM E CHAT ID, POI SALVA!
    echo ================================================================
    notepad ".env"
)

REM 3) Controlla cloudflared (scarica automaticamente se mancante)
where cloudflared >nul 2>nul
if %errorlevel% neq 0 (
    if not exist "cloudflared.exe" (
        echo [INFO] cloudflared.exe non trovato. Scarico la versione ufficiale Cloudflare...
        powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe' -UseBasicParsing; exit 0 } catch { exit 1 }"
        if %errorlevel% neq 0 (
            echo [AVVISO] Download automatico cloudflared fallito. Avvio solo in locale.
        ) else (
            echo [OK] cloudflared.exe scaricato con successo!
        )
    )
)

echo.
echo [AVVIO] Avvio server.py con Cloudflare Quick Tunnel...
echo         Appena pronto, il link HTTPS verra' mostrato qui e inviato su Telegram!
echo.
python server.py --tunnel
pause
