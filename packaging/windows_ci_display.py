"""Provision a supported display mode on an ephemeral GitHub Windows runner only.

Uses EnumDisplaySettingsW modes and ChangeDisplaySettingsW(CDS_TEST, then 0).
No registry persistence, custom modes, application zoom, or end-user execution.
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsw
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys


class DevMode(ctypes.Structure):
    # Fixed-width Win32 ABI, including the 16-byte printer/display union.
    _fields_ = [('device', ctypes.c_uint16*32), ('spec', ctypes.c_uint16),
                ('driver', ctypes.c_uint16), ('size', ctypes.c_uint16),
                ('extra', ctypes.c_uint16), ('fields', ctypes.c_uint32),
                ('position_union', ctypes.c_byte*16), ('color', ctypes.c_int16),
                ('duplex', ctypes.c_int16), ('y_resolution', ctypes.c_int16),
                ('tt_option', ctypes.c_int16), ('collate', ctypes.c_int16),
                ('form', ctypes.c_uint16*32), ('log_pixels', ctypes.c_uint16),
                ('bits', ctypes.c_uint32), ('width', ctypes.c_uint32),
                ('height', ctypes.c_uint32), ('flags', ctypes.c_uint32),
                ('frequency', ctypes.c_uint32), ('tail', ctypes.c_uint32*8)]


class WindowsDisplay:
    def __init__(self):
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.EnumDisplaySettingsW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(DevMode)]
        self.api.EnumDisplaySettingsW.restype = ctypes.c_int32
        self.api.ChangeDisplaySettingsW.argtypes = [ctypes.POINTER(DevMode), ctypes.c_uint32]
        self.api.ChangeDisplaySettingsW.restype = ctypes.c_int32
        self.enumerated = {}

    def read(self, index):
        mode = DevMode(); mode.size = ctypes.sizeof(mode)
        if not self.api.EnumDisplaySettingsW(None, index, ctypes.byref(mode)):
            return None
        data = {name:int(getattr(mode, name)) for name in ('width','height','bits','frequency')}
        self.enumerated[tuple(data.values())] = mode
        return data

    def current(self):
        data = self.read(0xffffffff)  # ENUM_CURRENT_SETTINGS
        if data is None: raise RuntimeError('Cannot read current native display mode')
        return data

    def modes(self):
        modes = []
        for index in range(4096):
            mode = self.read(index)
            if mode is None: return modes
            modes.append(mode)
        raise RuntimeError('Display mode enumeration exceeded bounded limit')

    def change(self, data, test):
        mode = self.enumerated[tuple(data.values())]
        return self.api.ChangeDisplaySettingsW(ctypes.byref(mode), 2 if test else 0)


def provision(host, width, height, report):
    fits = lambda mode: mode['width'] >= width and mode['height'] >= height and mode['bits'] >= 32
    report.update(before=host.current(), required={'width':width,'height':height}, attempts=[])
    try:
        report['supported_modes'] = host.modes()
        if fits(report['before']): return
        candidates = sorted((m for m in report['supported_modes'] if fits(m)),
                            key=lambda m:(m['width']*m['height'], -m['frequency']))
        for mode in candidates:
            attempt = {'mode':mode, 'test_result':host.change(mode, True)}
            report['attempts'].append(attempt)
            if attempt['test_result'] != 0: continue
            attempt['apply_result'] = host.change(mode, False)
            if attempt['apply_result'] == 0:
                if not fits(host.current()): raise RuntimeError('Native display still too small after successful mode change')
                return
        raise RuntimeError('No supported display mode could provide the required layout viewport')
    finally:
        report['after'] = host.current()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--minimum-width', type=int, required=True)
    parser.add_argument('--minimum-height', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'status':'failed', 'validation_sha':os.environ.get('SHAQ_LAYOUT_VALIDATION_SHA', os.environ.get('GITHUB_SHA'))}
    try:
        if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
            raise RuntimeError('Display changes are restricted to ephemeral GitHub-hosted Windows runners')
        provision(WindowsDisplay(), args.minimum_width, args.minimum_height, report)
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = str(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
