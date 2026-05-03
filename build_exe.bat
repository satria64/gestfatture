@echo off
echo ============================================
echo   GestFatture - Build EXE con PyInstaller
echo ============================================

:: Installa dipendenze
echo.
echo [1/3] Installazione dipendenze...
pip install -r requirements.txt
pip install pyinstaller

:: Build
echo.
echo [2/3] Compilazione in corso...
pyinstaller ^
  --onefile ^
  --noconsole ^
  --name "GestFatture" ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --icon NONE ^
  app.py

:: Risultato
echo.
echo [3/3] Fatto!
echo L'eseguibile si trova in: dist\GestFatture.exe
echo.
echo NOTA: al primo avvio si aprira' automaticamente il browser
echo       su http://127.0.0.1:5000
echo.
pause
