"""Repair only existing per-user links named for the running application."""
import json
import os
from pathlib import Path
import subprocess
import sys

from .background_process import background_process_options


def repair_shortcuts(executable: Path, roots=None):
    if sys.platform != 'win32':
        return {'repaired': 0}
    executable = executable.resolve(strict=True)
    # Values cross via environment, never interpolated into PowerShell source.
    script = r'''
$ErrorActionPreference='Stop'
$w=New-Object -ComObject WScript.Shell
$roots=if($env:SHAQ_LINK_ROOTS){@($env:SHAQ_LINK_ROOTS|ConvertFrom-Json)}else{
 @($w.SpecialFolders.Item('Desktop'),$w.SpecialFolders.Item('Programs'))
}
$count=0
$name=[IO.Path]::GetFileNameWithoutExtension($env:SHAQ_LINK_TARGET)+'.lnk'
foreach($root in $roots){
 if(-not(Test-Path -LiteralPath $root)){continue}
 foreach($file in Get-ChildItem -LiteralPath $root -Filter $name -File -Recurse){
  $link=$w.CreateShortcut($file.FullName)
  if($link.TargetPath -ne $env:SHAQ_LINK_TARGET){
   $link.TargetPath=$env:SHAQ_LINK_TARGET
   $link.WorkingDirectory=[IO.Path]::GetDirectoryName($env:SHAQ_LINK_TARGET)
   $link.IconLocation=$env:SHAQ_LINK_TARGET+',0'
   $link.Save();$count++
  }
 }
}
@{repaired=$count}|ConvertTo-Json -Compress
'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        env=dict(os.environ, SHAQ_LINK_TARGET=str(executable),
                 SHAQ_LINK_ROOTS=json.dumps([str(p) for p in roots]) if roots is not None else ''),
        capture_output=True, text=True, encoding='utf-8', timeout=15, check=True,
        **background_process_options())
    return json.loads(result.stdout)
