param([string]$DistPath = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -X utf8 -m unittest -v test_quota_sources.py test_tps_sources.py test_tray_app.py test_split_ring.py test_features.py test_claude.py test_claude_status.py test_mimo.py test_reset_watch.py test_agent_tasks.py test_agent_hints.py test_settings.py test_usage_store.py test_process_jail.py test_model_iq.py test_sync_hub.py test_app_identity.py
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
$claudeReceiver = Join-Path $root 'claude_status.ps1'
$appIcon = Join-Path $root 'assets\circlow-fox-tray-mascot.ico'
# The build's date and commit, shown in the settings sidebar and diagnostics.
New-Item -ItemType Directory -Force (Join-Path $root 'work') | Out-Null
$buildInfo = Join-Path $root 'work\build_info.json'
$commit = (git -C $root rev-parse --short HEAD 2>$null); if (-not $commit) { $commit = 'unknown' }
@{ commit = $commit; date = (Get-Date -Format 'yyyy-MM-dd HH:mm') } | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 $buildInfo
python -m PyInstaller --noconfirm --onefile --windowed `
  --collect-binaries winpty --collect-data winpty --collect-data tzdata `
  --add-data "$claudeReceiver;." --add-data "$buildInfo;." --add-data "$appIcon;." --icon $appIcon `
  --runtime-tmpdir (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime-temp') --name Circlow `
  --distpath $DistPath `
  --workpath work/pyinstaller `
  --specpath work `
  tray_app.py
if ($LASTEXITCODE -ne 0) { throw "Packaging failed" }

$exe = Join-Path (Resolve-Path -LiteralPath $DistPath).Path "Circlow.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "EXE was not produced" }
Get-Item -LiteralPath $exe | Select-Object FullName, Length, LastWriteTime
