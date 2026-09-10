"""Fail closed if PyTables tries to load Blosc2 from outside the frozen payload."""
from pathlib import Path
import sys


def check_blosc2_origin(event, args):
    if event == 'ctypes.dlopen' and args[0] and 'blosc2' in Path(args[0]).name.lower():
        library = Path(args[0])
        if not library.is_absolute() or not library.resolve().is_relative_to(Path(sys._MEIPASS).resolve()):
            raise RuntimeError('Blosc2 must load from the bundled native payload')


if getattr(sys, 'frozen', False):
    sys.addaudithook(check_blosc2_origin)
