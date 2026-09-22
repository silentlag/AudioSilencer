@echo off
rem === Build clean share package (no tokens) ===
cd /d %~dp0
rmdir /s /q share 2>nul
mkdir share
copy /y AudioSilencer.exe share\ >nul
copy /y app.ico share\ >nul
xcopy /e /i /y tosu share\tosu >nul
echo {"discord": {"client_id": "YOUR_CLIENT_ID", "client_secret": "YOUR_CLIENT_SECRET"}} > share\config.json
echo.
echo Done: share\
echo 	     1) create own app at discord.com/developers/applications
echo         2) OAuth2 tab - Redirects - add http://localhost:8000
echo         3) open share\config.json, replace YOUR_CLIENT_ID / YOUR_CLIENT_SECRET
echo            (paste - no keyboard layout switch needed)
echo         4) run AudioSilencer.exe, press Login Discord
pause