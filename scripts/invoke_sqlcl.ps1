#Requires -Version 5.1
# Shared SQLcl launcher for the PowerShell wrappers.
#
# SQLcl builds a JLine console over its standard input at startup. Handed a
# descriptor it cannot probe -- which is what a non-interactive PowerShell
# host, a redirected stream, or the NUL device gives it -- it aborts with
# "java.io.IOException: Incorrect function" before running the script. An
# empty regular file is a standard input every platform can probe, and it
# also stops SQLcl from consuming the caller's own input.
#
# Start-Process is what allows standard input to be redirected at all:
# Windows PowerShell 5.1 has no '<' redirection operator for native commands.

function Invoke-Sqlcl {
  param(
    [Parameter(Mandatory = $true)][string[]] $Arguments,
    [Parameter(Mandatory = $true)][string] $WorkingDirectory,
    [Parameter(Mandatory = $true)][string] $StdInFile,
    [string] $TranscriptFile
  )

  if (-not (Test-Path -LiteralPath $StdInFile -PathType Leaf)) {
    New-Item -ItemType File -Path $StdInFile | Out-Null
  }

  # Start-Process joins -ArgumentList with spaces and does not quote, so any
  # argument holding a space (a repository path, most often) must be quoted
  # here or SQLcl receives it as several arguments.
  $quoted = @($Arguments | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
  })

  # Start-Process' -WorkingDirectory resolves wildcard characters in this path.
  # Set the provider location by literal path and let the child inherit it.
  # Its redirected-input path is also wildcard-resolved, so hand it a system
  # temp copy even when the caller's checkout path contains brackets.
  $stdinRedirectFile = [System.IO.Path]::GetTempFileName()
  $locationPushed = $false
  try {
    Copy-Item -LiteralPath $StdInFile -Destination $stdinRedirectFile -Force
    Push-Location -LiteralPath $WorkingDirectory
    $locationPushed = $true
    if ([string]::IsNullOrWhiteSpace($TranscriptFile)) {
      $process = Start-Process -FilePath "sql" -ArgumentList $quoted `
        -NoNewWindow -Wait -PassThru -RedirectStandardInput $stdinRedirectFile
      return $process.ExitCode
    }

    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
      $process = Start-Process -FilePath "sql" -ArgumentList $quoted `
        -NoNewWindow -Wait -PassThru -RedirectStandardInput $stdinRedirectFile `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
      $stdout = [System.IO.File]::ReadAllText($stdoutFile)
      $stderr = [System.IO.File]::ReadAllText($stderrFile)
      $transcript = $stdout
      if ($stdout.Length -gt 0 -and $stderr.Length -gt 0) {
        $transcript += [Environment]::NewLine
      }
      $transcript += $stderr
      [System.IO.File]::WriteAllText($TranscriptFile, $transcript)
      return $process.ExitCode
    } finally {
      Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
  } finally {
    if ($locationPushed) { Pop-Location }
    Remove-Item -LiteralPath $stdinRedirectFile -Force -ErrorAction SilentlyContinue
  }
}
