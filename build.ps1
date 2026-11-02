param([string]$DistPath = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -X utf8 -m unittest -v test_quota_sources.py test_tps_sources.py test_tray_app.py test_split_ring.py test_features.py test_claude.py test_claude_status.py test_mimo.py test_reset_watch.py test_agent_tasks.py test_agent_hints.py test_settings.py test_usage_store.py test_process_jail.py test_model_iq.py
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
$claudeReceiver = Join-Path $root 'claude_status.ps1'
python -m PyInstaller --noconfirm --onefile --windowed `
  --collect-binaries winpty --collect-data winpty --collect-data tzdata `
  --add-data "$claudeReceiver;." `
  --runtime-tmpdir (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime-temp') --name CodexQuotaTray `
  --distpath $DistPath `
  --workpath work/pyinstaller `
  --specpath work `
  tray_app.py
if ($LASTEXITCODE -ne 0) { throw "Packaging failed" }

$exe = Join-Path (Resolve-Path -LiteralPath $DistPath).Path "CodexQuotaTray.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "EXE was not produced" }
Get-Item -LiteralPath $exe | Select-Object FullName, Length, LastWriteTime
