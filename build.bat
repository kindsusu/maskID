@echo off
REM ==========================================================================
REM  MarkID - PyInstaller build script (run by Fable, not by the implementer).
REM
REM  Produces a single, windowed, offline executable:  dist\MarkID.exe
REM  - bundles the Korean OCR model files (models\)
REM  - bundles all rapidocr_onnxruntime assets (its own det/cls models + config)
REM
REM  Build with the project virtual environment (created by the setup step):
REM      .venv\Scripts\activate            (then just call  pyinstaller ...)
REM  or invoke PyInstaller directly without activating:
REM      .venv\Scripts\pyinstaller.exe ...   (used below)
REM
REM  The build embeds the publisher identity + icon:
REM    --icon assets\MarkID.ico            program icon (multi-resolution .ico)
REM    --version-file version_info.txt     version resource (CompanyName=HRteam,
REM                                        LegalCopyright=Kim Kwon-su, ver 1.3.0.0)
REM ==========================================================================
setlocal
cd /d "%~dp0"

echo [MarkID] Building single-file executable...

.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed ^
  --name MarkID ^
  --icon assets\MarkID.ico ^
  --version-file version_info.txt ^
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
