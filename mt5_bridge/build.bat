@echo off
pip install -r requirements.txt
pyinstaller --onefile --windowed --name mt5_bridge mt5_bridge.py
echo.
echo Build complete. Find mt5_bridge.exe in the dist\ folder.
pause
