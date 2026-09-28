"""Entry point for the optional PyInstaller build (see packaging/build_pyinstaller.sh)."""

from trader.cli import app

if __name__ == "__main__":
    app()
