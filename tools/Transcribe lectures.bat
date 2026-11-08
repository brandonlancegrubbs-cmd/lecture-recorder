@echo off
rem Double-click after class: re-transcribes new lectures with ElevenLabs and copies them to the vault.
python "%~dp0transcribe_lectures.py" %*
pause
