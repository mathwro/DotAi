[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $DotAiArguments
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Root 'dotai.py'
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source -3 $Script @DotAiArguments
    exit $LASTEXITCODE
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction Stop }
& $python.Source $Script @DotAiArguments
exit $LASTEXITCODE
