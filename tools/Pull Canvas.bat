@echo off
rem Double-click to pull new Canvas slides, files, due dates and videos for every class (read-only).
rem Needs CANVAS_TOKEN set once: setx CANVAS_TOKEN "your-token"
where py >nul 2>nul
if %errorlevel%==0 (
  py "%~dp0canvas_pull.py" %*
) else (
  python "%~dp0canvas_pull.py" %*
)
pause
