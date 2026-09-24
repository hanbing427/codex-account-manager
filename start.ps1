param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ToolArgs)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$scriptPath = Join-Path $PSScriptRoot 'migrate.py'
$candidates = @()
$bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundled) { $candidates += $bundled }
foreach ($name in @('python.exe', 'python3.exe')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found -and $found.Source -notlike '*\WindowsApps\*') { $candidates += $found.Source }
}
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $candidate $scriptPath @ToolArgs
        exit $LASTEXITCODE
    }
}
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source -3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $py.Source -3 $scriptPath @ToolArgs
        exit $LASTEXITCODE
    }
}
Write-Host 'Python 3.11+ was not found. Install Python, or use the Codex bundled Python runtime.'
exit 1
