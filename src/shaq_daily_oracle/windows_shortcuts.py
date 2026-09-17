"""Repair existing per-user app shortcuts using the Unicode Shell interface.

IShellLinkW / IPersistFile (Microsoft Shell Links documentation). WScript's
legacy automation interface can corrupt non-ANSI paths on English Windows.
"""
import ctypes
from pathlib import Path
import sys
import uuid


def _shell_link(path: Path, target: Path | None = None) -> str:
    """Read or update a .lnk without resolving it or executing its target."""
    ole = ctypes.OleDLL('ole32')
    pointer = ctypes.c_void_p
    guid_type = ctypes.c_ubyte * 16

    def guid(value):
        return guid_type.from_buffer_copy(uuid.UUID(value).bytes_le)

    def check(result):
        if result < 0:
            raise ctypes.WinError(result)

    def call(obj, slot, types, *args):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, pointer, *types)(table[slot])
        check(method(obj, *args))

    # WinDLL keeps HRESULT visible, including an existing apartment mode.
    init = ctypes.WinDLL('ole32').CoInitializeEx
    init.argtypes = [pointer, ctypes.c_uint32]
    init.restype = ctypes.c_long
    initialized = init(None, 2)
    if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE
        check(initialized)
    link, persist = pointer(), pointer()
    try:
        clsid = guid('00021401-0000-0000-C000-000000000046')
        iid = guid('000214F9-0000-0000-C000-000000000046')  # IShellLinkW
        ole.CoCreateInstance.argtypes = [pointer, pointer, ctypes.c_uint32, pointer, pointer]
        ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(link))
        iid_file = guid('0000010B-0000-0000-C000-000000000046')
        call(link, 0, [pointer, pointer], ctypes.byref(iid_file), ctypes.byref(persist))
        if path.exists():
            call(persist, 5, [ctypes.c_wchar_p, ctypes.c_uint32], str(path), 0)
        elif target is None:
            raise FileNotFoundError(path)
        buffer = ctypes.create_unicode_buffer(32768)
        call(link, 3, [ctypes.c_wchar_p, ctypes.c_int, pointer, ctypes.c_uint32],
             buffer, len(buffer), None, 4)  # SLGP_RAWPATH; no target resolution
        previous = buffer.value
        if target is not None and previous.casefold() != str(target).casefold():
            call(link, 20, [ctypes.c_wchar_p], str(target))
            call(link, 9, [ctypes.c_wchar_p], str(target.parent))
            call(link, 17, [ctypes.c_wchar_p, ctypes.c_int], str(target), 0)
            call(persist, 6, [ctypes.c_wchar_p, ctypes.c_int], str(path), 1)
        return previous
    finally:
        for obj in (persist, link):
            if obj:
                call(obj, 2, [])  # IUnknown::Release
        if initialized >= 0:
            ole.CoUninitialize()


def repair_shortcuts(executable: Path, roots=None):
    if sys.platform != 'win32':
        return {'repaired': 0}
    executable = executable.resolve(strict=True)
    if roots is None:
        roots = []
        shell = ctypes.OleDLL('shell32')
        shell.SHGetFolderPathW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p]
        for folder in (0x10, 0x02):  # current user's DesktopDirectory / Programs
            buffer = ctypes.create_unicode_buffer(32768)
            shell.SHGetFolderPathW(None, folder, None, 0, buffer)
            roots.append(Path(buffer.value))
    count = 0
    for root in roots:
        for path in Path(root).rglob(executable.stem + '.lnk'):
            if path.is_file() and not path.is_symlink():
                previous = _shell_link(path, executable)
                count += previous.casefold() != str(executable).casefold()
    return {'repaired': count}
