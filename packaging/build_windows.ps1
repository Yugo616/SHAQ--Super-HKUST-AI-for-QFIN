param([switch]$Diagnostic, [switch]$UpstreamFailed)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonBin = if ($env:SHAQ_BUILD_PYTHON) { $env:SHAQ_BUILD_PYTHON } else { "python" }
Set-Location $ProjectRoot
$BuildArguments = @("packaging/windows_delivery.py")
if ($Diagnostic) { $BuildArguments += "--diagnostic" }
if ($UpstreamFailed) { $BuildArguments += "--upstream-failed" }
& $PythonBin @BuildArguments
exit $LASTEXITCODE
