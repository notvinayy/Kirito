@echo off
cd /d C:\Kirito
:loop
echo [%date% %time%] Launching KIRITO...
python kirito.py
echo [%date% %time%] Bot disconnected. Restarting in 5s...
timeout /t 5 >nul
goto loop