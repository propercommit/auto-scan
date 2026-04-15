#!/usr/bin/env python3
"""Entry point for the packaged Auto Scan application.

PyInstaller bundles this as the main script. It launches the Flask
GUI server and opens the browser — same as `auto-scan-gui` but with
extra handling for frozen (packaged) environments.
"""

import os
import sys


def _setup_frozen_env():
    """Configure paths for a PyInstaller-frozen environment."""
    if getattr(sys, "frozen", False):
        # Running as packaged app
        base = sys._MEIPASS  # PyInstaller temp directory
        # Ensure the bundled modules are importable
        if base not in sys.path:
            sys.path.insert(0, base)

        # Set a sensible working directory (user's home, not the temp dir)
        os.chdir(os.path.expanduser("~"))

        # Create default .env if it doesn't exist
        env_dir = os.path.expanduser("~/.auto_scan")
        os.makedirs(env_dir, exist_ok=True)
        env_file = os.path.join(env_dir, ".env")
        if not os.path.exists(env_file):
            with open(env_file, "w") as f:
                f.write("# Auto Scan configuration\n")
                f.write("# ANTHROPIC_API_KEY=sk-ant-...\n")
                f.write("# SCANNER_IP=192.168.1.100\n")
                f.write("# OUTPUT_DIR=~/Documents/Scans\n")

        # Load the app's .env from the persistent location
        from dotenv import load_dotenv
        load_dotenv(env_file)
        # Also try project-level .env
        load_dotenv(os.path.join(os.path.expanduser("~"), ".env"))


def main():
    _setup_frozen_env()
    from auto_scan.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
