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

# The live processes a Windows process started, and theirs (the launcher sql.exe starts
# java.exe), each as a Process object. Windows reuses process ids and leaves a dead
# parent's id on its children, so a process whose ParentProcessId matches is only a
# descendant if it started after that parent; anything older belongs to an earlier owner
# of the id (explorer.exe, for one). Each one is opened before its own children are
# looked up, which keeps its id from being reused meanwhile. The caller keeps the root's
# id the same way, by holding its handle.
function Get-SqlclDescendants([int] $RootId, [datetime] $RootStartTime) {
  $found = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
  $parents = [System.Collections.Generic.Queue[object]]::new()
  $parents.Enqueue(@($RootId, $RootStartTime))
  while ($parents.Count -gt 0) {
    $parent = $parents.Dequeue()
    try {
      $candidates = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($parent[0])" -ErrorAction SilentlyContinue)
    } catch {
      $candidates = @()
    }
    foreach ($candidate in $candidates) {
      if ($null -eq $candidate.CreationDate -or $candidate.CreationDate -lt $parent[1]) { continue }
      try {
        $child = [System.Diagnostics.Process]::GetProcessById([int] $candidate.ProcessId)
        $null = $child.Handle
        # The id was reused between the query and the open: not the process found.
        if ([Math]::Abs(($child.StartTime - $candidate.CreationDate).TotalMilliseconds) -ge 1) { continue }
      } catch {
        continue  # it has ended
      }
      $found.Add($child)
      $parents.Enqueue(@($child.Id, $child.StartTime))
    }
  }
  return $found.ToArray()
}

# End a process and everything it started, through the handles found (taskkill /T would
# pick its children by ParentProcessId alone).
function Stop-SqlclTree([System.Diagnostics.Process] $Process, [datetime] $StartTime) {
  foreach ($member in @($Process) + @(Get-SqlclDescendants $Process.Id $StartTime)) {
    try { if (-not $member.HasExited) { $member.Kill() } } catch { }
  }
}

# Wait for each process until one shared deadline, then end what is left with its tree.
function Stop-SqlclChildren([System.Diagnostics.Process[]] $Children, [datetime] $Deadline) {
  foreach ($child in $Children) {
    try {
      $remaining = [int][Math]::Max(0, ($Deadline - [datetime]::UtcNow).TotalMilliseconds)
      if (-not $child.WaitForExit($remaining)) {
        Stop-SqlclTree $child $child.StartTime
      }
    } catch { }
  }
}

# Wait for a process that Start-Process started without -Wait. Start-Process -Wait stops
# waiting the moment Ctrl-C arrives, and the caller's finally block then removes the
# directory SQLcl is still shutting down in (and cannot, on Windows, while SQLcl is in
# it). Wait in short steps instead; Stop-SqlclProcess handles an interrupt.
function Wait-SqlclProcess {
  param([Parameter(Mandatory = $true)] $Process)
  while (-not $Process.WaitForExit(200)) { }
  if (($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows)) {
    # The launcher can end before the java.exe it started; give its children ten
    # seconds in all, as after an interrupt, instead of waiting for ever.
    $savedExitCode = $global:LASTEXITCODE
    Stop-SqlclChildren -Children (Get-SqlclDescendants $Process.Id $Process.StartTime) -Deadline ([datetime]::UtcNow.AddSeconds(10))
    $global:LASTEXITCODE = $savedExitCode
  }
}

# After an interrupt: hold on until SQLcl has ended, and end it with everything it
# started if it takes longer than ten seconds.
function Stop-SqlclProcess {
  param([Parameter(Mandatory = $true)] $Process)
  # The caller reads $LASTEXITCODE to tell an interrupt from a failure.
  $savedExitCode = $global:LASTEXITCODE
  $deadline = [datetime]::UtcNow.AddSeconds(10)
  if (($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows)) {
    $children = Get-SqlclDescendants $Process.Id $Process.StartTime
    if (-not $Process.WaitForExit(10000)) {
      Stop-SqlclTree $Process $Process.StartTime
    }
    Stop-SqlclChildren -Children $children -Deadline $deadline
  } elseif (-not $Process.WaitForExit(10000)) {
    # Kill(true) ends the launcher's descendants too (the JVM a shell launcher started).
    try { $Process.Kill($true) } catch { }
  }
  $global:LASTEXITCODE = $savedExitCode
}

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
  if (Test-Path -LiteralPath (Join-Path $WorkingDirectory "login.sql") -PathType Leaf) {
    throw "refusing to start SQLcl in a directory containing login.sql: $WorkingDirectory"
  }

  # Start-Process joins -ArgumentList with spaces and does not quote, so any
  # argument holding a space (a repository path, most often) must be quoted
  # here or SQLcl receives it as several arguments.
  $quoted = @($Arguments | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
  })

  # Start-Process resolves the working directory as a wildcard pattern: when none
  # is given it uses the current location, and Windows PowerShell 5.1 then fails
  # with "Unable to find the specified file" if that path contains brackets (a
  # checkout under "C:\work [1]"). Pass the directory explicitly with the
  # wildcard characters escaped; PowerShell 5.1 and 7 both read that as the
  # literal path. The redirected-input path is also wildcard-resolved, so hand it
  # a system temp copy even when the caller's checkout path contains brackets.
  $escapedWorkingDirectory = [System.Management.Automation.WildcardPattern]::Escape($WorkingDirectory)
  $stdinRedirectFile = [System.IO.Path]::GetTempFileName()
  $safeSqlPath = Join-Path $WorkingDirectory ".sqlcl-path"
  [System.IO.Directory]::CreateDirectory($safeSqlPath) | Out-Null
  $hadSqlPath = Test-Path Env:SQLPATH
  $oldSqlPath = $env:SQLPATH
  $hadOraclePath = Test-Path Env:ORACLE_PATH
  $oldOraclePath = $env:ORACLE_PATH
  $locationPushed = $false
  # Set once SQLcl is started and once it was waited for to the end: a finally block
  # that sees a process not waited for was interrupted, wherever the Ctrl-C landed.
  $process = $null
  $waited = $false
  try {
    Copy-Item -LiteralPath $StdInFile -Destination $stdinRedirectFile -Force
    Push-Location -LiteralPath $WorkingDirectory
    $locationPushed = $true
    $env:SQLPATH = $safeSqlPath
    $env:ORACLE_PATH = $safeSqlPath
    if ([string]::IsNullOrWhiteSpace($TranscriptFile)) {
      $process = Start-Process -FilePath "sql" -ArgumentList $quoted -WorkingDirectory $escapedWorkingDirectory `
        -NoNewWindow -PassThru -RedirectStandardInput $stdinRedirectFile
      $null = $process.Handle  # Windows PowerShell 5.1 loses the exit code of a -PassThru process otherwise
      Wait-SqlclProcess $process
      $waited = $true
      return $process.ExitCode
    }

    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
      $process = Start-Process -FilePath "sql" -ArgumentList $quoted -WorkingDirectory $escapedWorkingDirectory `
        -NoNewWindow -PassThru -RedirectStandardInput $stdinRedirectFile `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
      $null = $process.Handle
      Wait-SqlclProcess $process
      $waited = $true
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
      if ($null -ne $process -and -not $waited) {
        Stop-SqlclProcess $process  # before its output files are removed
        $waited = $true
      }
      Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
  } finally {
    if ($null -ne $process -and -not $waited) { Stop-SqlclProcess $process }
    if ($hadSqlPath) { $env:SQLPATH = $oldSqlPath } else { Remove-Item Env:SQLPATH -ErrorAction SilentlyContinue }
    if ($hadOraclePath) { $env:ORACLE_PATH = $oldOraclePath } else { Remove-Item Env:ORACLE_PATH -ErrorAction SilentlyContinue }
    if ($locationPushed) { Pop-Location }
    Remove-Item -LiteralPath $stdinRedirectFile -Force -ErrorAction SilentlyContinue
  }
}

# Return the parsing schema that owns an APEX application, or throw.
#
# A lookup only reads, so a transient failure to start SQLcl (Start-Process can
# throw "Broken pipe" while it feeds stdin to a process that has already exited)
# or to capture its output (the transcript can still be empty when SQLcl has
# just exited) is retried instead of being reported as a lookup failure. A real
# SQLcl or database error, and an application that does not exist, are never
# retried.
function Get-AppParsingSchema {
  param(
    [Parameter(Mandatory = $true)][string] $Connection,
    [Parameter(Mandatory = $true)][string] $ExpectedUser,
    [Parameter(Mandatory = $true)][string] $Schema,
    [Parameter(Mandatory = $true)][string] $AppId,
    [Parameter(Mandatory = $true)][string] $WorkDirectory,
    [Parameter(Mandatory = $true)][string] $ScriptPath,
    [string] $Environment = $env:DB_ENVIRONMENT
  )
  $errorPattern = '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:'
  $resultPattern = "(?m)^\s*APEX_APP_SCHEMA:$([regex]::Escape($AppId)):(.*?)\s*$"
  $problem = "the parsing-schema lookup for application $AppId returned no result"
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    $attemptDirectory = Join-Path $WorkDirectory "attempt$attempt"
    [System.IO.Directory]::CreateDirectory($attemptDirectory) | Out-Null
    $transcript = Join-Path $attemptDirectory "lookup-output.log"
    try {
      $exit = Invoke-Sqlcl -WorkingDirectory $attemptDirectory `
        -StdInFile (Join-Path $attemptDirectory ".sqlcl-stdin") -TranscriptFile $transcript `
        -Arguments @("-S", "-noupdates", "-name", $Connection, "@$ScriptPath", $Schema, $AppId, $Environment, $ExpectedUser)
    } catch {
      $problem = "SQLcl could not be run for the lookup of application ${AppId}: $($_.Exception.Message)"
      continue
    }
    $text = ""
    for ($read = 0; $read -lt 10; $read++) {
      if (Test-Path -LiteralPath $transcript -PathType Leaf) { $text = [System.IO.File]::ReadAllText($transcript) }
      if ($exit -ne 0 -or $text -match $errorPattern -or $text -match $resultPattern) { break }
      Start-Sleep -Milliseconds 200
    }
    if ($exit -ne 0 -or $text -match $errorPattern) {
      throw "could not look up the parsing schema of application ${AppId}:`n$text"
    }
    $match = [regex]::Match($text, $resultPattern)
    if ($match.Success) {
      $owner = $match.Groups[1].Value
      if ($owner -eq "NOT_FOUND") { throw "application $AppId was not found in the workspace visible to this connection" }
      return $owner
    }
    $problem = "the parsing-schema lookup for application $AppId returned no result"
  }
  throw $problem
}
