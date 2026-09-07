$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OutputRoot = Join-Path $ProjectRoot "dist\desktop-windows"
$AppName = "SHAQ Daily Oracle Lab"
$PythonBin = if ($env:SHAQ_BUILD_PYTHON) { $env:SHAQ_BUILD_PYTHON } else { "python" }

New-Item -ItemType Directory -Force (Join-Path $ProjectRoot "dist") | Out-Null

Set-Location $ProjectRoot
& $PythonBin -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --name $AppName `
  --collect-all webview `
  --collect-all pandas_market_calendars `
  --collect-all yfinance `
  --collect-all keyring `
  --hidden-import openai `
  --hidden-import keyring.backends.Windows `
  --add-data "$ProjectRoot\pyproject.toml;." `
  --add-data "$ProjectRoot\config;config" `
  --add-data "$ProjectRoot\governance;governance" `
  --add-data "$ProjectRoot\schemas;schemas" `
  --add-data "$ProjectRoot\skills;skills" `
  --add-data "$ProjectRoot\src\shaq_daily_oracle\desktop;shaq_daily_oracle\desktop" `
  --distpath $OutputRoot `
  --workpath (Join-Path $ProjectRoot "build\desktop-windows") `
  --specpath (Join-Path $ProjectRoot "build") `
  (Join-Path $ProjectRoot "packaging\desktop_entry.py")

$Executable = Join-Path $OutputRoot "$AppName\$AppName.exe"
& $Executable --smoke
if ($LASTEXITCODE -ne 0) { throw "Packaged desktop smoke test failed" }

$Compiler = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Compiler)) { throw "Inno Setup 6 is required" }
& $Compiler "/DProjectRoot=$ProjectRoot" (Join-Path $ProjectRoot "packaging\windows-installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Windows installer build failed" }
$Installer = Join-Path $ProjectRoot "dist\SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe"
$Digest = (Get-FileHash $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
$Sidecar = "$Installer.sha256"
$Line = "$Digest  $(Split-Path -Leaf $Installer)`n"
[System.IO.File]::WriteAllText($Sidecar, $Line, [System.Text.UTF8Encoding]::new($false))
$SidecarBytes = [System.IO.File]::ReadAllBytes($Sidecar)
if ($SidecarBytes -contains 13) { throw "SHA-256 sidecar must use portable LF line endings" }
