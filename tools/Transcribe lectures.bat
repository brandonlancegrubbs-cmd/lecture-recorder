@echo off
rem Double-click after class: re-transcribes new lectures with ElevenLabs and copies them to the vault.
where py >nul 2>nul
if %errorlevel%==0 (
  py "%~dp0transcribe_lectures.py" %*
) else (
  python "%~dp0transcribe_lectures.py" %*
)
pause
