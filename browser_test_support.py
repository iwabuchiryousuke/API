"""Locate an existing browser for offline fixtures on Windows or Linux."""
import os
from pathlib import Path
import shutil


def browser_executable(playwright):
    explicit = os.environ.get('SBI_TEST_BROWSER')
    if explicit:
        if not Path(explicit).is_file():raise RuntimeError('SBI_TEST_BROWSER does not point to a browser executable')
        return explicit
    for name in ('chromium', 'chromium-browser', 'msedge', 'google-chrome', 'chrome'):
        found = shutil.which(name)
        if found:return found
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        root = os.environ.get(variable)
        if not root:continue
        for relative in ('Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'):
            candidate = Path(root) / relative
            if candidate.is_file():return str(candidate)
    bundled = Path(playwright.chromium.executable_path)
    return str(bundled) if bundled.is_file() else None
