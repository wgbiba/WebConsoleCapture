@echo off
REM Starts Chrome with the remote debugging port enabled, using a
REM separate profile so it doesn't conflict with normal Chrome.
REM --remote-allow-origins=* permits Chrome 111+ to accept WebSocket
REM connections from the logger app.
set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME% set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
start "" %CHROME% --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="%USERPROFILE%\chrome-debug"
