#Requires -Version 5.1
# Refresh table, code and (when configured) ORDS mirrors through independent
# read targets. -OrdsOnly is backup-ords: only the ORDS export, installed as
# database/<SCHEMA>/ords so the table and code mirrors stay as they are.
param([switch] $OrdsOnly)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
. (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")

# A failed SPOOL inside the generated driver prints an SP2- message that does
# not stop SQLcl, so an object can go missing without any non-zero exit code.
# The manifest states how many objects each scope should have produced; refuse
# to install a mirror that does not have exactly that many files.
function Get-ScopeDirectory {
  param([Parameter(Mandatory = $true)][ValidateSet("tables", "code")][string] $Scope)
  if ($Scope -eq "tables") { return @("tables") }
  return @("views", "packages", "procedures", "functions", "triggers", "synonyms")
}

function Get-SqlclSpoolSchemaName {
  param([Parameter(Mandatory = $true)][string] $Schema)
  # URL-safe Base64 is reversible, contains no SQLcl-significant '$', and is
  # short enough for a maximum-length Oracle identifier as one path component.
  $bytes = [System.Text.Encoding]::ASCII.GetBytes($Schema)
  $encoded = [Convert]::ToBase64String($bytes).TrimEnd([char[]]@('='))
  return ".sqlcl-schema-$($encoded.Replace('+', '-').Replace('/', '_'))"
}

function Test-ScopeComplete {
  param(
    [string] $Scope,
    [string] $Schema,
    [string] $StagingPath
  )
  $spoolSchema = Get-SqlclSpoolSchemaName -Schema $Schema
  $manifestPath = Join-Path $StagingPath "database/$spoolSchema/manifest-$Scope.txt"
  $expected = 0
  $counted = 0
  $manifestTypes = @()
  foreach ($line in (Get-Content -LiteralPath $manifestPath)) {
    $separator = $line.LastIndexOf('=')
    if ($separator -lt 0) { continue }
    $manifestTypes += $line.Substring(0, $separator)
    $count = $line.Substring($separator + 1).Trim()
    $parsed = 0
    if ([int]::TryParse($count, [ref] $parsed)) {
      $expected += $parsed
      $counted += 1
    }
  }

  # No parsable counts at all means the manifest itself is unusable. Fail
  # closed rather than approving whatever happens to be staged.
  if ($counted -eq 0) {
    throw ("database backup manifest for $Schema ($Scope) has no readable " +
      "object counts; the mirror was not replaced")
  }

  if ($Scope -eq "tables") {
    $requiredTypes = @("TABLE")
  } else {
    $requiredTypes = @("VIEW", "PACKAGE", "PACKAGE BODY", "PROCEDURE", "FUNCTION", "SYNONYM", "TRIGGER")
  }
  foreach ($requiredType in $requiredTypes) {
    if ($manifestTypes -cnotcontains $requiredType) {
      throw "database backup manifest for $Schema ($Scope) is missing the $requiredType row; the mirror was not replaced"
    }
  }

  $scopeDirs = Get-ScopeDirectory -Scope $Scope
  $actual = 0
  foreach ($scopeDir in $scopeDirs) {
    $scopePath = Join-Path $StagingPath "database/$spoolSchema/$scopeDir"
    if (Test-Path -LiteralPath $scopePath) {
      $actual += @(Get-ChildItem -LiteralPath $scopePath -File -Filter *.sql).Count
    }
  }

  if ($expected -ne $actual) {
    throw ("database backup is incomplete for $Schema ($Scope): manifest expects " +
      "$expected object file(s) but $actual were written; the mirror was not replaced")
  }
}

function Split-BackupList([string] $Value) {
  # A function's output is unrolled, so callers must wrap the call in @( ):
  # that restores an array for zero or one entries. (Do not use `return ,@(...)`;
  # it emits the array as ONE object, and @( ) would then count it as one entry.)
  if ([string]::IsNullOrEmpty($Value)) { return }
  return $Value.Split(',')
}
$backupTargets = @()
foreach ($backupProfile in @(
    @{ Scope = "tables"; Schemas = $env:TABLES_SCHEMA; Connections = $env:TABLES_SQLCL_CONNECTION; Users = $env:TABLES_EXPECTED_USER; Prefixes = $env:TABLES_PREFIXES },
    @{ Scope = "code"; Schemas = $env:CODE_SCHEMA; Connections = $env:CODE_SQLCL_CONNECTION; Users = $env:CODE_EXPECTED_USER; Prefixes = $env:CODE_PREFIXES }
  )) {
  $schemaList = @(Split-BackupList $backupProfile.Schemas)
  $connectionList = @(Split-BackupList $backupProfile.Connections)
  $userList = @(Split-BackupList $backupProfile.Users)
  for ($index = 0; $index -lt $schemaList.Count; $index++) {
    $backupTargets += [PSCustomObject]@{
      Scope = $backupProfile.Scope
      Schema = $schemaList[$index]
      Connection = $connectionList[$index]
      ExpectedUser = $userList[$index]
      Prefixes = $backupProfile.Prefixes
    }
  }
}
# ORDS is optional. The loader leaves these empty when the profile is not
# configured, and, under --schema, when the profile does not list the schema.
$ordsTargets = @()
$ordsSchemaList = @(Split-BackupList $env:ORDS_SCHEMA)
$ordsConnectionList = @(Split-BackupList $env:ORDS_SQLCL_CONNECTION)
$ordsUserList = @(Split-BackupList $env:ORDS_EXPECTED_USER)
for ($index = 0; $index -lt $ordsSchemaList.Count; $index++) {
  $ordsTargets += [PSCustomObject]@{
    Scope = "ords"
    Schema = $ordsSchemaList[$index]
    Connection = $ordsConnectionList[$index]
    ExpectedUser = $ordsUserList[$index]
  }
}
if ($OrdsOnly) {
  $backupTargets = @()
  if ($env:PROJECT_ORDS_CONFIGURED -ne "true") {
    throw "backup-ords error: ORDS is not configured; set ORDS_SCHEMA, ORDS_SQLCL_CONNECTION and ORDS_EXPECTED_USER in .env"
  }
  if ($ordsTargets.Count -eq 0) {
    throw "backup-ords error: the ORDS profile does not list schema $($env:PROJECT_SCHEMA)"
  }
}
if ($backupTargets.Count -eq 0 -and $ordsTargets.Count -eq 0) { throw "backup error: no profile lists schema $($env:PROJECT_SCHEMA); nothing to back up" }
foreach ($target in @($backupTargets) + @($ordsTargets)) {
  & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target $target.Scope -Schema $target.Schema
}
$backupSchemas = @((@($backupTargets) + @($ordsTargets)) | ForEach-Object { $_.Schema } | Select-Object -Unique)
# A schema whose table or code scope runs is replaced as a whole mirror
# (database/<SCHEMA>). A schema that only has an ORDS scope in this run is not:
# only database/<SCHEMA>/ords is replaced, so mirrors this run did not export
# survive untouched.
$databaseSchemas = @($backupTargets | ForEach-Object { $_.Schema } | Select-Object -Unique)
$ordsSchemas = @($ordsTargets | ForEach-Object { $_.Schema })

# Refuse local mirror edits before making either database connection.
foreach ($schema in $backupSchemas) {
  $destination = if ($databaseSchemas -ccontains $schema) { "database/$schema" } else { "database/$schema/ords" }
  # Windows PowerShell 5.1 ends the script on any native stderr text (such as
  # git's "could not open directory" warning) under $ErrorActionPreference = "Stop".
  $previousErrorPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $dirty = @(git -C $repoRoot status --porcelain --untracked-files=all -- $destination 2>$null)
  } finally {
    $ErrorActionPreference = $previousErrorPreference
  }
  if ($LASTEXITCODE -ne 0) { throw "unable to inspect Git status for mirror: $destination" }
  if (-not [string]::IsNullOrWhiteSpace(($dirty -join "`n"))) {
    throw "refusing to back up over dirty mirror: $destination; commit, stash, or remove local changes first"
  }
}

$scratchPath = Join-Path $repoRoot "scratch"
# New-Item has no -LiteralPath parameter on either Windows PowerShell 5.1 or
# PowerShell 7. The .NET API is literal and has the same create-if-missing behavior.
[System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null
$stagingPath = Join-Path $scratchPath ("db-backup-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path (Join-Path $stagingPath "scripts") | Out-Null

try {
  $locationPushed = $false
  Push-Location -LiteralPath $stagingPath
  $locationPushed = $true

  # An unsupported SQLcl release fails here, before any export session opens.
  if ($ordsTargets.Count -gt 0) {
    Assert-SqlclOrdsVersion -WorkDirectory (Join-Path $stagingPath "sqlcl-version")
  }

  # Every export and manifest must complete before any generated mirror changes.
  foreach ($target in $backupTargets) {
    $spoolSchema = Get-SqlclSpoolSchemaName -Schema $target.Schema
    foreach ($scopeDir in (Get-ScopeDirectory -Scope $target.Scope)) {
      New-Item -ItemType Directory -Force `
        -Path (Join-Path $stagingPath "database/$spoolSchema/$scopeDir") | Out-Null
    }
    $transcriptPath = Join-Path $stagingPath ".sqlcl-transcript.txt"
    # The SQLcl launcher on Windows expands an unquoted * against the working
    # directory, which shifts every later argument. backup_db.sql treats % as the
    # same "every object" value, and % cannot occur in an identifier prefix.
    $prefixArgument = $target.Prefixes
    if ($prefixArgument -ceq "*" -and (($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows))) {
      $prefixArgument = "%"
    }
    $sqlclExit = Invoke-Sqlcl -WorkingDirectory $stagingPath `
      -StdInFile (Join-Path $stagingPath ".sqlcl-stdin") `
      -TranscriptFile $transcriptPath `
      -Arguments @(
        "-S", "-noupdates", "-name", $target.Connection,
        "@$(Join-Path $repoRoot 'scripts/backup_db.sql')",
        $target.Schema, $target.Scope, $env:DB_ENVIRONMENT,
        $target.ExpectedUser, $prefixArgument, $spoolSchema
      )
    # backup_db.sql sets LONG far above SQLcl's ~2 MB warning threshold so the
    # largest package body is never truncated; drop that advisory, keep the rest.
    if (Test-Path -LiteralPath $transcriptPath -PathType Leaf) {
      $longAdvisory = @(
        "Warning: This LONG setting may cause Java memory problems.",
        "It is recommended to reduce the setting and/or increase the memory available to Java."
      )
      foreach ($line in [System.IO.File]::ReadAllLines($transcriptPath)) {
        if ($longAdvisory -cnotcontains $line) { Write-Output $line }
      }
      Remove-Item -LiteralPath $transcriptPath -Force -ErrorAction SilentlyContinue
    }
    if ($sqlclExit -ne 0) {
      throw "SQLcl $($target.Scope) metadata backup failed with exit code $sqlclExit"
    }
    $manifestPath = Join-Path $stagingPath "database/$spoolSchema/manifest-$($target.Scope).txt"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
      throw "database backup did not create manifest-$($target.Scope).txt for $($target.Schema)"
    }
    Test-ScopeComplete -Scope $target.Scope -Schema $target.Schema -StagingPath $stagingPath
  }

  # One schema's ORDS export, judged by scripts/ords_export.py: SQLcl's exit
  # status and a non-empty spool file prove nothing, so identity, completion,
  # the dictionary inventory and consistency between two exports are all checked.
  foreach ($target in $ordsTargets) {
    $spoolSchema = Get-SqlclSpoolSchemaName -Schema $target.Schema
    New-Item -ItemType Directory -Force -Path (Join-Path $stagingPath "database/$spoolSchema/ords") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $stagingPath "verify/$spoolSchema") | Out-Null
    $ordsTranscript = Join-Path $stagingPath "ords-transcript-$spoolSchema.txt"
    $sqlclExit = Invoke-Sqlcl -WorkingDirectory $stagingPath `
      -StdInFile (Join-Path $stagingPath ".sqlcl-stdin") `
      -TranscriptFile $ordsTranscript `
      -Arguments @(
        "-S", "-noupdates", "-name", $target.Connection,
        "@$(Join-Path $repoRoot 'scripts/ords_export.sql')",
        $target.Schema, $env:DB_ENVIRONMENT, $target.ExpectedUser, $spoolSchema
      )
    $longAdvisory = @(
      "Warning: This LONG setting may cause Java memory problems.",
      "It is recommended to reduce the setting and/or increase the memory available to Java."
    )
    $kept = @()
    if (Test-Path -LiteralPath $ordsTranscript -PathType Leaf) {
      foreach ($line in [System.IO.File]::ReadAllLines($ordsTranscript)) {
        if ($longAdvisory -cnotcontains $line) { Write-Output $line; $kept += $line }
      }
      [System.IO.File]::WriteAllText($ordsTranscript, (($kept -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    }
    if ($sqlclExit -ne 0) {
      throw "ORDS export for $($target.Schema) failed in SQLcl with exit code $sqlclExit; the mirror was not replaced"
    }
    $verified = Invoke-OrdsExportHelper -HelperArguments @(
      "verify", "--stage", $stagingPath, "--spool-schema", $spoolSchema,
      "--schema", $target.Schema, "--expected-user", $target.ExpectedUser,
      "--transcript", $ordsTranscript
    )
    if ($verified.Status -ne 0) { throw "$($verified.Text)`nthe mirror was not replaced" }
    Write-Output $verified.Text
  }

  Pop-Location
  $locationPushed = $false
  $replaceArgs = @()
  foreach ($schema in $backupSchemas) {
    $spoolSchema = Get-SqlclSpoolSchemaName -Schema $schema
    if ($databaseSchemas -ccontains $schema) {
      Move-Item -LiteralPath (Join-Path $stagingPath "database/$spoolSchema") `
        -Destination (Join-Path $stagingPath "database/$schema")
      $liveOrds = Join-Path $repoRoot "database/$schema/ords"
      if (($ordsSchemas -cnotcontains $schema) -and (Test-Path -LiteralPath $liveOrds)) {
        # The whole schema mirror is replaced, and this run did not export ORDS
        # for the schema: carry its committed ORDS export over unchanged. Any
        # edit made since the dirty-mirror check is caught by the recheck in
        # replace_mirror, which refuses a dirty mirror.
        Copy-Item -LiteralPath $liveOrds -Destination (Join-Path $stagingPath "database/$schema/ords") -Recurse
      }
      # A scope that produced no objects of one type leaves an empty directory
      # that would otherwise be installed. Prune after verification. Descending
      # order empties the deepest directories first, so a parent left empty by
      # its own pruned children is removed in the same pass.
      Get-ChildItem -LiteralPath (Join-Path $stagingPath "database/$schema") -Recurse -Directory |
        Sort-Object -Property FullName -Descending |
        ForEach-Object {
          if (-not (Get-ChildItem -LiteralPath $_.FullName -Force)) {
            Remove-Item -LiteralPath $_.FullName -Force
          }
        }
      $replaceArgs += (Join-Path $stagingPath "database/$schema")
      $replaceArgs += "database/$schema"
    } else {
      $installParent = Join-Path $stagingPath "ords-install"
      [System.IO.Directory]::CreateDirectory($installParent) | Out-Null
      Move-Item -LiteralPath (Join-Path $stagingPath "database/$spoolSchema/ords") `
        -Destination (Join-Path $installParent $schema)
      $replaceArgs += (Join-Path $installParent $schema)
      $replaceArgs += "database/$schema/ords"
    }
  }
  & (Join-Path $PSScriptRoot "replace_mirror.ps1") @replaceArgs
} finally {
  if ($locationPushed) { Pop-Location }
  if (Test-Path -LiteralPath $stagingPath) {
    # This runs after the mirrors are already installed. A file handle Windows
    # has not released yet must not convert a completed backup into a failure,
    # which is why Bash uses `rm -rf` in a trap and ignores the result.
    Remove-Item -LiteralPath $stagingPath -Recurse -Force -ErrorAction SilentlyContinue
  }
}
