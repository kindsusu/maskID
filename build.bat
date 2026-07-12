@echo off
REM ==========================================================================
REM  MarkID - PyInstaller build script (run by Fable, not by the implementer).
REM
REM  Produces a single, windowed, offline executable:  dist\MarkID.exe
REM  - bundles the Korean OCR model files (models\)
REM  - bundles all rapidocr_onnxruntime assets (its own det/cls models + config)
REM
REM  Prerequisite (install once):   pip install pyinstaller
REM ==========================================================================
setlocal
cd /d "%~dp0"

echo [MarkID] Building single-file executable...

pyinstaller --noconfirm --clean --onefile --windowed ^
  --name MarkID ^
  --collect-all rapidocr_onnxruntime ^
  --add-data "models;models" ^
  --exclude-module matplotlib ^
  --exclude-module tkinter.test ^
  run_markid.py

if errorlevel 1 (
  echo.
  echo [MarkID] BUILD FAILED.
  exit /b 1
)

echo.
echo [MarkID] Build complete:  dist\MarkID.exe
echo [MarkID] Quick check:     dist\MarkID.exe --selftest
endlocal
