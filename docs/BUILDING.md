# Building & packaging

## Run from source

```bash
python -m venv .venv
.venv\Scripts\activate     # Windows
pip install -r requirements.txt
python -m app
```

## One-click Windows EXE

```bat
BUILD_EXE.bat
```

Internally runs:

```
pyinstaller --noconfirm --onefile --windowed ^
  --name WebConsoleCapture ^
  --icon app\assets\icon.ico ^
  --add-data "app\assets;app\assets" ^
  --collect-submodules PySide6 ^
  --collect-submodules websocket ^
  --collect-submodules rapidocr_onnxruntime ^
  app\__main__.py
```

Output: `dist\WebConsoleCapture.exe`.

## macOS / Linux

Use the same PyInstaller invocation but swap `--add-data` syntax:

```bash
pyinstaller --onefile --windowed \
  --name WebConsoleCapture \
  --add-data "app/assets:app/assets" \
  app/__main__.py
```

## Notes

- The first time OCR mode runs, RapidOCR downloads ~10 MB of ONNX models.
- The build's final size depends on whether OCR is included. Strip it
  via `--exclude-module rapidocr_onnxruntime` for a smaller CDP-only build.
