param(
  [Parameter(Mandatory = $true)]
  [string]$InstallDir
)

$ErrorActionPreference = "SilentlyContinue"
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

try {
  $installRoot = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd("\") + "\"
} catch {
  exit 0
}

$processNames = @("qwenpaw-backend.exe", "qwenpaw.exe")

$targets = foreach ($processName in $processNames) {
  Get-CimInstance Win32_Process -Filter "Name = '$processName'" |
    Where-Object {
      if (-not $_.ExecutablePath) {
        return $false
      }

      try {
        $processPath = [System.IO.Path]::GetFullPath($_.ExecutablePath)
      } catch {
        return $false
      }

      return $processPath.StartsWith(
        $installRoot,
        [System.StringComparison]::OrdinalIgnoreCase
      )
    }
}

$processIds = @($targets | ForEach-Object { $_.ProcessId } | Sort-Object -Unique)

foreach ($processId in $processIds) {
  Stop-Process -Id $processId -Force
}

$timedOut = $false
if ($processIds.Count -gt 0) {
  try {
    Wait-Process -Id $processIds -Timeout 8 -ErrorAction Stop
  } catch {
    $timedOut = $true
  }
}

$Stopwatch.Stop()
Write-Output "QwenPaw backend sidecar stop: matched=$($processIds.Count) timed_out=$timedOut elapsed_ms=$($Stopwatch.ElapsedMilliseconds)"

exit 0
