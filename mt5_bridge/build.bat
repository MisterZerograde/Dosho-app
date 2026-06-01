@echo off
echo Installing dependencies...
pip install MetaTrader5 flask flask-cors pystray Pillow pyinstaller

echo.
echo Building mt5_bridge.exe...
pyinstaller ^
  --onefile ^
  --windowed ^
  --name mt5_bridge ^
  --hidden-import pystray._win32 ^
  mt5_bridge.py

echo.
if exist dist\mt5_bridge.exe (
  echo SUCCESS: dist\mt5_bridge.exe is ready.
  explorer dist
) else (
  echo BUILD FAILED — check the output above.
)
pause
