@echo off
chcp 65001 >nul 2>&1
echo ============================================
echo   GitMergeMods — Сборка .exe
echo ============================================
echo.

REM Проверяем Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ОШИБКА: Python не найден. Установите Python 3.8+ с python.org
    pause
    exit /b 1
)

REM Устанавливаем PyInstaller если нет
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo Установка PyInstaller...
    pip install pyinstaller
)

echo.
echo Сборка GitMergeMods.exe ...
echo.

pyinstaller ^
    --onefile ^
    --windowed ^
    --name GitMergeMods ^
    --clean ^
    --noconfirm ^
    main.py

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo   ✅ Сборка завершена!
    echo   Файл: dist\GitMergeMods.exe
    echo ============================================
) else (
    echo.
    echo ❌ Ошибка сборки!
)

echo.
pause
