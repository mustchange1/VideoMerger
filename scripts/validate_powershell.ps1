[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Scripts = @(
    (Join-Path $Root 'setup_windows.ps1'),
    (Join-Path $Root 'run_windows.ps1'),
    (Join-Path $Root 'diagnostics_windows.ps1'),
    (Join-Path $Root 'test_windows.ps1'),
    (Join-Path $Root 'uninstall_windows.ps1'),
    (Join-Path $Root 'scripts\package_windows.ps1')
)
$Failed = $false
foreach ($ScriptPath in $Scripts) {
    $Tokens = $null
    $Errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$Tokens, [ref]$Errors) | Out-Null
    if ($Errors.Count -gt 0) {
        $Failed = $true
        Write-Host ('SYNTAX FAIL: ' + $ScriptPath) -ForegroundColor Red
        foreach ($ParseError in $Errors) {
            Write-Host ('  ' + $ParseError.Message + ' at ' + $ParseError.Extent.StartLineNumber + ':' + $ParseError.Extent.StartColumnNumber) -ForegroundColor Red
        }
    }
    else {
        Write-Host ('SYNTAX PASS: ' + $ScriptPath) -ForegroundColor Green
    }
}
if ($Failed) { exit 1 }
exit 0
