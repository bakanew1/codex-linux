param([string]$Python="python")
$ErrorActionPreference="Stop"
$proxyRoot=Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $proxyRoot
try {
    & $Python -E -s -c "import broker,client_mcp,common,bulk_read,process_guard,private_storage,remote_gate; print('LOCAL_IMPORT_OK')"
    $proxyCode=$LASTEXITCODE
} finally {Pop-Location}
exit $proxyCode
