$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$desktop = $shell.SpecialFolders.Item('Desktop')
$shortcutName = 'Codex Account Manager.lnk'
$shortcut = $shell.CreateShortcut((Join-Path $desktop $shortcutName))
$shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'Start-Web.vbs') + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = (Join-Path $PSScriptRoot '..\app\ui\account-manager-v2.ico') + ',0'
$shortcut.Description = 'Codex Account Manager'
$shortcut.Save()
$previousPath = Join-Path $desktop (([string][char]0x661f + [char]0x67a2) + ' - Codex.lnk')
if (Test-Path -LiteralPath $previousPath) {
    $previous = $shell.CreateShortcut($previousPath)
    if ($previous.WorkingDirectory -eq $PSScriptRoot -and $previous.TargetPath -eq $shortcut.TargetPath) {
        Remove-Item -LiteralPath $previousPath
    }
}
Write-Host 'Desktop shortcut created: Codex Account Manager'
