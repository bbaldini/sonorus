@echo off
rem Prepara l'ambiente di Sonorus e crea il collegamento sul Desktop.
cd /d %~dp0
set PYTHONIOENCODING=utf-8
if not exist .venv\Scripts\python.exe (
  py -3.12 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist sonorus.ico .venv\Scripts\python tools\make_icon.py
rem scarica il modello (circa 1,6 GB, solo la prima volta)
if not "%SONORUS_SKIP_MODEL%"=="1" .venv\Scripts\python -c "import engine; from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8', download_root=str(engine.MODEL_DIR))"
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\Sonorus.lnk\"); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='\"%~dp0app.py\"'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0sonorus.ico'; $s.Description='Trascrizione in diretta'; $s.Save()"
echo.
echo Fatto: trovi Sonorus sul Desktop.
