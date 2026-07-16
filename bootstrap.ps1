[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $DotAiArguments
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
        if ($LASTEXITCODE -eq 0) { return @($py.Source, '-3') }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
        if ($LASTEXITCODE -eq 0) { return @($python.Source) }
    }

    $candidatePatterns = @(
        "$HOME\scoop\apps\python\current\python.exe",
        "$env:SCOOP\apps\python\current\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe"
    )
    $candidates = Get-ChildItem $candidatePatterns -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
    foreach ($candidate in $candidates) {
        & $candidate.FullName -c "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
        if ($LASTEXITCODE -eq 0) { return @($candidate.FullName) }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
        throw 'Python 3.10+ is missing and Scoop is unavailable. Install Scoop from https://scoop.sh and retry.'
    }
    scoop install python
    if ($LASTEXITCODE -ne 0) { throw "Scoop failed to install Python (exit $LASTEXITCODE)." }
    $Python = Find-Python
}

if (-not $Python) { throw 'Python 3.10+ was not found after installation.' }

$Executable = $Python[0]
$Prefix = @()
if ($Python.Count -gt 1) { $Prefix = $Python[1..($Python.Count - 1)] }
& $Executable @Prefix "$Root\dotai.py" install @DotAiArguments
exit $LASTEXITCODE
