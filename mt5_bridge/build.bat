@echo off
echo Installing dependencies...
pip install MetaTrader5 customtkinter pyinstaller

echo.
echo Building mt5_sync.exe...
pyinstaller ^
  --onefile ^
  --windowed ^
  --name mt5_sync ^
  --hidden-import customtkinter ^
  --collect-all customtkinter ^
  mt5_sync.py

echo.
if exist dist\mt5_sync.exe (
  echo SUCCESS: dist\mt5_sync.exe is ready.
  explorer dist
) else (
  echo BUILD FAILED — check the output above.
)
pause
