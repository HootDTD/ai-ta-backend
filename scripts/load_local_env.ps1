# Dot-source this in a PowerShell terminal to load .env + .env.local into the
# CURRENT session, then run any backend process in that same terminal. The
# workers (teacher_upload_worker / apollo.provision_worker / apollo.learner_
# janitor_worker) do NOT load .env themselves, so they rely on this.
#
#   . .\scripts\load_local_env.ps1
#   python -m teacher_upload_worker
#
# .env provides secrets (OPENAI_API_KEY, ...); .env.local (loaded second, wins)
# provides local Supabase/Neo4j URLs + feature flags. .env is never modified.

function Import-DotEnv($path) {
  if (-not (Test-Path $path)) { Write-Host "skip (not found): $path"; return }
  Get-Content $path | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#')) { return }
    $idx = $line.IndexOf('=')
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$key" -Value $val
  }
}

$root = Join-Path $PSScriptRoot '..'
Import-DotEnv (Join-Path $root '.env')        # secrets first
Import-DotEnv (Join-Path $root '.env.local')  # local overrides win

Write-Host "Loaded .env + .env.local."
Write-Host "  SUPABASE_URL    = $($env:SUPABASE_URL)"
Write-Host "  SUPABASE_DB_URL = $($env:SUPABASE_DB_URL)"
Write-Host "  NEO4J_URI       = $($env:NEO4J_URI)"
Write-Host "  APOLLO_AUTOPROVISION_ENABLED = $($env:APOLLO_AUTOPROVISION_ENABLED)"
if (-not $env:OPENAI_API_KEY) { Write-Warning "OPENAI_API_KEY is empty - check .env" }
