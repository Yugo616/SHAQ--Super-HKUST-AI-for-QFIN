$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OutputRoot = Join-Path $ProjectRoot "dist\desktop-windows"
$AppName = "SHAQ Daily Oracle"

Set-Location $ProjectRoot
python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --name $AppName `
  --collect-all webview `
  --collect-all pandas_market_calendars `
  --hidden-import keyring.backends.Windows `
  --add-data "$ProjectRoot\pyproject.toml;." `
  --add-data "$ProjectRoot\config;config" `
  --add-data "$ProjectRoot\governance;governance" `
  --add-data "$ProjectRoot\schemas;schemas" `
  --add-data "$ProjectRoot\scripts;scripts" `
  --add-data "$ProjectRoot\skills;skills" `
  --add-data "$ProjectRoot\tests;tests" `
  --add-data "$ProjectRoot\src\shaq_daily_oracle;src\shaq_daily_oracle" `
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
Get-FileHash (Join-Path $ProjectRoot "dist\SHAQ-Daily-Oracle-Windows-x64-Setup.exe") -Algorithm SHA256 |
  Format-List | Out-File (Join-Path $ProjectRoot "dist\SHAQ-Daily-Oracle-Windows-x64-Setup.exe.sha256")
