"""Platform launch options for captured, noninteractive desktop child processes."""
import subprocess
import sys


def background_process_options():
    # The fallback is the Windows API constant, used by cross-platform tests.
    # No shell is introduced; the caller still supplies an argument list.
    return ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
            if sys.platform == 'win32' else {})
