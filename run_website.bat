@echo off
cd /d "%~dp0webapp"
echo Starting the vacant land toolkit website...
echo Once it says "Running on http://127.0.0.1:5000", open that address in your browser.
"C:\Users\catti\AppData\Local\Programs\Python\Python312\python.exe" app.py
pause
