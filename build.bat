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

REM Проверяем pip
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ОШИБКА: pip не найден в текущем Python.
    pause
    exit /b 1
)

REM Устанавливаем PyInstaller если нет
python -m pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo Установка PyInstaller...
    python -m pip install pyinstaller
)

REM Устанавливаем Dulwich если нет
python -m pip show dulwich >nul 2>&1
if %errorlevel% neq 0 (
    echo Установка Dulwich...
    python -m pip install dulwich
)

REM Устанавливаем truststore если нет
python -m pip show truststore >nul 2>&1
if %errorlevel% neq 0 (
    echo Установка truststore...
    python -m pip install truststore
)

REM Устанавливаем certifi если нет
python -m pip show certifi >nul 2>&1
if %errorlevel% neq 0 (
    echo Установка certifi...
    python -m pip install certifi
)

echo.
echo Сборка GitMergeMods.exe ...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name GitMergeMods ^
    --clean ^
    --noconfirm ^
    --collect-data certifi ^
    --hidden-import truststore ^
    --hidden-import dulwich ^
    --hidden-import dulwich.porcelain ^
    --hidden-import dulwich.client ^
    --hidden-import dulwich.protocol ^
    --hidden-import dulwich.pack ^
    --hidden-import dulwich.repo ^
    --hidden-import dulwich.object_store ^
    --hidden-import urllib3 ^
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
