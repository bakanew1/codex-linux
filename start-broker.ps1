param(
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [string]$Python="python"
)
$ErrorActionPreference="Stop"
$proxyRoot=Split-Path -Parent $PSScriptRoot
if(-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)){throw "Private config does not exist."}
& $Python -E -s (Join-Path $proxyRoot "broker.py") serve --config $ConfigPath
exit $LASTEXITCODE
