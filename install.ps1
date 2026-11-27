# Kuding Desktop MCP public installer. Compatible with Windows PowerShell 5.1.
param(
  [string]$Dir = (Join-Path $env:LOCALAPPDATA 'KudingDesktopMCP'),
  [string]$UvExecutable = 'uv',
  [string]$IndexUrl = '',
  [string]$ExcludeTools = 'PowerShell,Registry,FileSystem,Process,Scrape',
  [int]$SelfTestWait = 60
)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:OS -ne 'Windows_NT') { throw 'Windows is required.' }
$Dir = [IO.Path]::GetFullPath($Dir)
$marker = Join-Path $Dir '.kuding-desktop-mcp'
if ((Test-Path -LiteralPath $Dir) -and -not (Test-Path -LiteralPath $marker)) {
  if (@(Get-ChildItem -LiteralPath $Dir -Force).Count -gt 0) {
    throw 'The destination is not an existing Kuding installation or an empty directory.'
  }
}
$uvCommand = Get-Command $UvExecutable -ErrorAction Stop
$uvPath = $uvCommand.Source
New-Item -ItemType Directory -Path $Dir -Force | Out-Null
Set-Content -LiteralPath $marker -Value 'Kuding Desktop MCP' -Encoding ASCII
$py = Join-Path $Dir 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
  & $uvPath venv --python 3.13 (Join-Path $Dir 'venv')
  if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
$env:ANONYMIZED_TELEMETRY = 'false'
$env:KUDING_DESKTOP_HOME = $Dir
$indexArgs = @()
if ($IndexUrl) { $indexArgs = @('--index-url', $IndexUrl) }
# --no-deps is deliberate: requirements.lock enumerates the tested dependency
# set and omits upstream's PostHog SDK. Never launch the unpatched upstream.
& $uvPath pip install --python $py --no-deps --require-hashes @indexArgs -r (Join-Path $Here 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; the server was not started.' }
& $uvPath pip uninstall --python $py posthog
if ($LASTEXITCODE -ne 0) { throw 'Could not remove the telemetry dependency.' }
& $py (Join-Path $Here 'scripts\install_runtime.py') --source $Here --destination $Dir --exclude-tools $ExcludeTools
if ($LASTEXITCODE -ne 0) { throw 'Runtime preparation failed.' }
& $py (Join-Path $Dir 'selftest.py') --timeout $SelfTestWait
if ($LASTEXITCODE -ne 0) { throw 'MCP self-test failed. Review its output before using this installation.' }
Write-Host 'Installation and MCP self-test completed.'
Write-Host ('Configuration: ' + (Join-Path $Dir 'mcp.json'))
Write-Host 'Merge that entry into your client configuration; then restart that client.'
