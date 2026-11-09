@echo off
rem One-time setup: runs auto_lectures.py every 10 minutes while you're logged in.
rem It transcribes finished lectures (ElevenLabs, capped at $10 a month), matches them to the slides,
rem and fills in the Lectures tab on the study guides site. Run "Turn off auto lectures.bat" to stop it.
where pyw >nul 2>nul
if %errorlevel%==0 (
  set "RUNNER=pyw"
) else (
  set "RUNNER=pythonw"
)
schtasks /Create /F /SC MINUTE /MO 10 /TN "Lecture auto" /TR "%RUNNER% \"%~dp0auto_lectures.py\"" >nul
if %errorlevel%==0 (
  echo Turned on: "Lecture auto" runs every 10 minutes while you're logged in.
  echo Running it once now so you can see it work...
  py "%~dp0auto_lectures.py"
  echo.
  echo The log is in Fall2026\_auto_log.txt and spending in Fall2026\_elevenlabs_spend.json.
) else (
  echo Couldn't create the scheduled task. Right-click this file and choose "Run as administrator", then try again.
)
pause
