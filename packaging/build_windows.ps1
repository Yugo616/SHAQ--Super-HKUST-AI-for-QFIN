$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonBin = if ($env:SHAQ_BUILD_PYTHON) { $env:SHAQ_BUILD_PYTHON } else { "python" }
Set-Location $ProjectRoot
& $PythonBin packaging/build_desktop.py --output dist/desktop-windows
if ($LASTEXITCODE -ne 0) { throw "Complete app build failed" }
$Executable = Join-Path $ProjectRoot "dist\desktop-windows\SHAQ Daily Oracle Lab\SHAQ Daily Oracle Lab.exe"
$Smoke = Start-Process $Executable -ArgumentList ('--smoke --smoke-output "{0}"' -f "$ProjectRoot\dist\smoke.json") -Wait -PassThru
if ($Smoke.ExitCode -ne 0) { throw "Packaged two-method settlement smoke failed" }
& $PythonBin packaging/audit_payload.py "dist/desktop-windows/SHAQ Daily Oracle Lab" --output dist/native-audit.json
if ($LASTEXITCODE -ne 0) { throw "Native payload audit failed" }
$Compiler = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Compiler)) { throw "Inno Setup 6 is required on the build runner" }
$AppVersion = & $PythonBin -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
& $Compiler "/DProjectRoot=$ProjectRoot" "/DAppVersion=$AppVersion" packaging/windows-installer.iss
if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
$Installer = Join-Path $ProjectRoot "dist\SHAQ-Daily-Oracle-Lab-Windows-x64-Setup.exe"
$Digest = (Get-FileHash $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText("$Installer.sha256", "$Digest  $(Split-Path -Leaf $Installer)`n", [System.Text.UTF8Encoding]::new($false))
