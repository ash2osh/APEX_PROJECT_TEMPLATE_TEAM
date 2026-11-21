#Requires -Version 5.1
# No param block: a [Parameter()] attribute makes this an advanced script, and an
# advanced script exits 0 after Ctrl-C whatever its finally block says. $args also
# keeps option-like tokens (--schema=X, -x) that the parameter binder would take
# for parameters, so the first token is always the command, as in team.sh.
$Command = if ($args.Count -gt 0) { [string] $args[0] } else { "" }
$Arguments = @($args | Select-Object -Skip 1 | ForEach-Object { [string] $_ })

$ErrorActionPreference = "Stop"
# Start from a known status: the finally block at the bottom reads it, and the
# caller's session may still hold the status of an earlier command.
$global:LASTEXITCODE = 0

# Set once the command has ended by its own means: it returned, failed, or chose
# an exit status. The finally block at the bottom treats any other end as Ctrl-C.
$finished = $false

# --schema and the targets command change PROJECT_SCHEMA for this run only. The caller's
# value (null when unset) is kept here and put back before every exit, so a persistent
# PowerShell session does not see the previous command's selection.
$script:callerProjectSchema = [Environment]::GetEnvironmentVariable("PROJECT_SCHEMA")
$script:callerSchemaRestored = $false

function Restore-CallerProjectSchema {
  if ($script:callerSchemaRestored) { return }
  $script:callerSchemaRestored = $true
  if ($null -eq $script:callerProjectSchema) {
    Remove-Item -LiteralPath Env:PROJECT_SCHEMA -ErrorAction SilentlyContinue
  } else {
    [Environment]::SetEnvironmentVariable("PROJECT_SCHEMA", $script:callerProjectSchema)
  }
}

function Fail([string] $Message) {
  [Console]::Error.WriteLine("team error: $Message")
  $script:finished = $true
  Restore-CallerProjectSchema
  exit 2
}

function Exit-Team([int] $Code) {
  $script:finished = $true
  Restore-CallerProjectSchema
  exit $Code
}

# No command prints only the everyday commands; --help prints all of them.
function Show-EverydayUsage {
  @"
Usage: scripts/team.ps1 <command> [arguments]

Everyday commands:
  doctor                                  Check .env and the database connections
  export <app_id>                         Copy a live app from DEV into Git
  publish <app_id> --env dev              Import your edited app files to DEV
  check-conflicts <folder> --env dev      Check a migration against the live database
  migrate <folder> --env dev              Apply a migration
  revise <folder>                         Start the next revision of an attempted migration
  archive-migrations <folder> [...]       Preview migration folders to archive
  backup-db                               Refresh the local database mirror
  upgrade-template --dry-run              Preview a template upgrade

Run scripts/team.ps1 --help for every command and option.
"@ | Write-Output
}

function Show-Usage {
  @"
Usage: scripts/team.ps1 <command> [arguments]

Commands:
  doctor                                      Validate .env and the DEV SQLcl identities (ORDS too, when configured)
  targets [--format text|json]                Show the schema, connection and login each operation uses (offline; never connects)
  export <app_id>                             Export one numeric APEX app from DEV
  upgrade-apexlang <app_id> --env dev --mode builder|files
                                              Convert source explicitly to canonical APEX 26.2
  publish <app_id> [--env dev] [--force] [--strict-bytes] [--no-restore-automations]
                                              Drift-check, restore active automations and import to DEV
  publish <app_id> --file pages/<file>.apx [--file ...] [--no-team-notice] [--no-restore-automations]
                                              Verify selected existing DEV pages and synchronize source
  app-lock <app_id> [--env dev] [--comment <text>]
                                              Acquire a native DEV application lock
  app-unlock <app_id> [--env dev]             Release only your DEV application lock
  check-conflicts <folder> [...] (--env <env>|--local)
                                              Preflight selected migrations against local/live scope
  migration-plan [<folder> ...] [--live --env <env>] [--schema NAME] [--format text|json|markdown]
                                              Inventory selected migrations or run a guarded read-only preflight
  dev-test [--plan|--run|--report RUN_ID|--cleanup RUN_ID] [--env dev] [--schema NAME]
           [--suite FILE] [--scenario ID ...] [--format text|json|markdown]
                                              Plan, run, inspect or explicitly clean retained DEV fixtures
  new-migration <name> --source TYPE:NAME [...] --env dev
                                              Start a code migration from the live DEV source
  migrate <folder> [...] --env dev|staging|prod [--verbose] [--rehearse] [--report <file>] [--confirm-target <db_unique_name>] [--yes|-y]
                                              Apply, or rehearse DML in one transaction and roll it back
  revise <folder> [--reason TEXT]             Copy to the next migration revision
  revise --check <folder>                     Report whether local attempt/receipt evidence locks a folder
  archive-migrations <folder> [...] [--schema SCHEMA] [--destination DIR] [--reason TEXT] [--yes|-y]
  archive-migrations --all [--keep <folder>...] [--schema SCHEMA] [--destination DIR] [--reason TEXT] [--yes|-y]
           [--preserve-unresolved] [--skip-refused]
                                              Move released folders outside migrations/ (dry run by default)
  reconcile-migration <folder> --env dev|staging|prod --outcome <outcome> --evidence <file>
           [--attempt <id>] [--superseded-by <folder>] [--yes|-y]
                                              Record the owner's resolution of a pending attempt (local files only)
  verify <folder> [...] --env <env> [--phase pre|post|both] [--only-failed]
         [--format text|json] [--jobs N]     Evaluate migration checks read-only
  query --env <env> --connection <profile> [--file <path> | SQL]
                                              Run one bounded, conservative SELECT read-only
  rollout <manifest.json> --env <env> [--from-step N] [--dry-run] [--prepare]
         [--capsule DIR] [--stage-report FILE] [--recover UUID] [--report <file>]
         [--yes|-y] [--confirm-target <db_unique_name>]
                                              Confirm and execute a frozen deployment manifest
  feature <init|capture|apply|diff|doctor|freeze|clean-capsule|reconcile-run|convert-plan|release-plan> ...
                                              Create mutable feature sources and freeze version-2 release capsules;
                                              reconcile-run closes an unfinished run; convert-plan plans offline
  compare-schema [--from <env>] (--to <env>|--env <env>)
                (--object <name>|--pattern <glob>) [...] [--format text|json]
                                              Compare selected live schema objects read-only
  compare-env --from <env> --to <env> [--section <name>] [...] [--format text|json|markdown]
              [--emit-dba-script <file>]
                                              Compare complete environment catalogs by name, read-only
  baseline export-source --from <env> [--scratch <dir>]
                                              Export configured exact stored source and settings
  baseline export-grants --from <env> [--scratch <dir>]
                                              Export configured object and system grants
  baseline export-data --from <env> [--scratch <dir>]
                                              Export allow-listed reference rows without excluded columns
  baseline build --to <env> [--from <env>] [--data] [--bundle <name>]
                                              Build baseline migrations, optionally bundling structure, grants, and source
  baseline filter-ords --exclude-module NAME [...] --input <file> --output <file>
                                              Remove complete named modules from an ORDS export
  backup-db                                   Refresh the table and code mirrors (and ORDS, when configured)
  backup-ords                                 Export ORDS metadata read-only to database/<SCHEMA>/ords/schema.sql
  deploy <app_id> --env <staging|prod> [--manual] [--strict-bytes] [--no-restore-automations]
         [--yes|-y] [--confirm-target <db_unique_name>]
                                              Confirm a promotion or print a DBA runbook
  upgrade-template [--source <url|path>] [--ref <ref>] [--apex-release 26.1|26.2] [--dry-run]
                                              Update template-owned files from the template
  verify-local [--format text|json] [--skills-root <path>] [...] [--live]
                                              Validate local environment, lock, apps and skills read-only
  mcp setup [--choice chrome|firefox|both|none] [--yes|-y]
                                              Choose browser MCP servers for this checkout
  mcp status                                  Show the saved browser MCP choice
  skills setup none|superpowers|mattpocock|both [--primary superpowers|mattpocock]
                                              Set up optional repository workflow packs
  skills status [--format text|json]          Inspect repository workflow packs offline
  template-status [--format text|json] [--upstream --ref <ref>]
                                              Inspect local template drift; fetch upstream only when requested
  automations list|check|enable|disable|run-once <app_id> --env <env> --schema <schema>
         [--yes|-y] [--confirm-target <db_unique_name>]
                                              Observe, check or explicitly operate on one scoped automation
  staging-neutralize --capture|--check|--apply|--restore FILE [--confirm-target <db_unique_name>] [--yes|-y]
                                              Report, check, disable or undo a restored staging copy's jobs,
                                              APEX automations and network grants (DBA alias)
Options:
  --schema <NAME>                             Run one configured schema (except offline/tooling commands)
  --help                                      Show this help
Environment:
  MIGRATION_PREFLIGHT_INVENTORY_RETRIES        Retry changing live catalogs (default 3)
  <ENV>_DBA_SQLCL_CONNECTION                   Read-only compare-env and baseline grant-export connection
"@ | Write-Output
}

# The Bash that runs the migration, comparison and deployment helpers. TEAM_BASH
# names one explicitly. On Windows, bash.exe in System32 or WindowsApps is the
# WSL launcher, which cannot run a Windows script path, and Git for Windows
# keeps only its cmd directory on PATH by default, so Git Bash is looked up
# next to git.exe and in the usual install directories.
function Resolve-TeamBash {
  if (-not [string]::IsNullOrWhiteSpace($env:TEAM_BASH)) {
    if (-not (Test-Path -LiteralPath $env:TEAM_BASH -PathType Leaf)) {
      Fail "TEAM_BASH is set but is not a file: $($env:TEAM_BASH)"
    }
    return $env:TEAM_BASH
  }
  $onWindows = ($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows)
  $found = @(Get-Command bash -All -ErrorAction SilentlyContinue)
  if (-not $onWindows) {
    if ($found.Count -gt 0) { return $found[0].Source }
    return $null
  }
  foreach ($command in $found) {
    if ($command.Source -notmatch '(?i)[\\/](System32|SysWOW64|Sysnative|WindowsApps)[\\/]') { return $command.Source }
  }
  $git = Get-Command git -ErrorAction SilentlyContinue
  if ($null -ne $git) {
    $gitDirectory = Split-Path -Parent $git.Source
    foreach ($relative in @("..\bin\bash.exe", "..\..\bin\bash.exe", "..\usr\bin\bash.exe", "..\..\usr\bin\bash.exe")) {
      $candidate = [System.IO.Path]::GetFullPath((Join-Path $gitDirectory $relative))
      if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
  }
  foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Programs" }))) {
    if ([string]::IsNullOrWhiteSpace($root)) { continue }
    $candidate = Join-Path $root "Git\bin\bash.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
  }
  return $null
}

# The migration scripts accept only migrations/<folder> with forward slashes. On
# Windows a folder argument is naturally typed or tab-completed as
# .\migrations\<folder>\ (or pasted as an absolute path), so turn that into the
# repository-relative form. Option tokens and anything outside the checkout are
# left alone; the Python validator still refuses what it cannot accept.
function ConvertTo-MigrationFolderArgument([string] $Value) {
  $onWindows = ($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows)
  if (-not $onWindows -or $Value.StartsWith("-")) { return $Value }
  $converted = $Value
  try {
    if ([System.IO.Path]::IsPathRooted($converted)) {
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path.TrimEnd("\", "/")
      $full = [System.IO.Path]::GetFullPath($converted)
      if ($full.StartsWith($repoRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        $converted = $full.Substring($repoRoot.Length + 1)
      }
    }
  } catch {
    # Windows PowerShell 5.1's .NET throws on a character no Windows path may hold (such as a
    # double quote). It is no folder: leave the text for the validator, which refuses it.
  }
  $converted = $converted.Replace("\", "/")
  while ($converted.StartsWith("./")) { $converted = $converted.Substring(2) }
  return $converted.TrimEnd("/")
}

# Git Bash reports a process that a signal ended as the signal number times 256 (Ctrl-C,
# SIGINT, is 512), which a Windows caller reads as a plain exit status. Bash itself
# says 128 plus the signal number (130), so do the same.
function ConvertFrom-GitBashStatus([int] $Status) {
  if ($Status -ge 256 -and $Status -le 32512 -and $Status % 256 -eq 0) { return 128 + [int] ($Status / 256) }
  return $Status
}

# Windows PowerShell 5.1, and PowerShell 7 before 7.3, build a native command line with the
# legacy rules: an empty argument disappears, an embedded double quote is dropped, and a value
# with a space that ends in a backslash closes its quote early, so the arguments after it are
# swallowed. PowerShell 7.3 and newer pass them intact unless $PSNativeCommandArgumentPassing
# is set to Legacy, or is "Windows" (its default on Windows) and the program is one that Windows
# keeps on the legacy rules: a .bat, .cmd, .js, .vbs or .wsf file, or cmd.exe, cscript.exe,
# wscript.exe, find.exe or sqlcmd.exe. TargetPath is the program that is actually started; its
# name is compared case-insensitively.
function Test-LegacyNativeArgumentPassing([string] $TargetPath) {
  if ($PSVersionTable.PSEdition -eq "Desktop") { return $true }
  $version = $PSVersionTable.PSVersion
  if (($version.Major -lt 7) -or (($version.Major -eq 7) -and ($version.Minor -lt 3))) { return $true }
  $mode = [string] (Get-Variable -Name PSNativeCommandArgumentPassing -ValueOnly -ErrorAction SilentlyContinue)
  if ($mode -eq "Legacy") { return $true }
  if ($mode -ne "Windows") { return $false }
  $fileName = [System.IO.Path]::GetFileName([string] $TargetPath)
  $extension = [System.IO.Path]::GetExtension($fileName)
  return ((@(".bat", ".cmd", ".js", ".vbs", ".wsf") -contains $extension) -or
    (@("cmd.exe", "cscript.exe", "wscript.exe", "find.exe", "sqlcmd.exe") -contains $fileName))
}

# Refuses, before anything starts, a value that the legacy rules would garble for the named
# native command. The values are checked as the caller passes them, so a folder argument that
# ConvertTo-MigrationFolderArgument already converted is checked in that converted form. The
# value is not echoed: it may be long SQL, and naming the rule is enough.
function Assert-NativeArguments([string] $Target, [string] $TargetPath, [string[]] $Values) {
  if (-not (Test-LegacyNativeArgumentPassing $TargetPath)) { return }
  foreach ($value in @($Values)) {
    $text = [string] $value
    $endsInBackslash = $text.EndsWith("\", [System.StringComparison]::Ordinal)
    if (($text.Length -eq 0) -or $text.Contains('"') -or (($text -match '\s') -and $endsInBackslash)) {
      Fail "Windows PowerShell 5.1 cannot pass an empty argument, a double quote (`"), or a value with a space that ends in a backslash to $Target intact (nor can PowerShell before 7.3, with `$PSNativeCommandArgumentPassing set to Legacy, or through a .cmd or .bat launcher): run this command with PowerShell 7.3 or newer (pwsh) and a program that is not a .cmd or .bat file, or run scripts/team.sh in Git Bash"
    }
  }
}

function Invoke-TeamBash {
  param([string] $ScriptName, [string[]] $ScriptArguments)
  $bashPath = Resolve-TeamBash
  if ($null -eq $bashPath) {
    Fail "Bash is required for '$ScriptName'; install Git for Windows, set TEAM_BASH to its bash.exe, or run scripts/team.sh"
  }
  if (($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows)) {
    # Git Bash's launcher reads ' in the command line as a quote: the argument arrives without
    # it and joined with the next one, which fails far from the cause.
    foreach ($forwarded in @(Join-Path $PSScriptRoot $ScriptName) + @($ScriptArguments)) {
      if ($forwarded.Contains("'")) {
        Fail "Git Bash cannot receive an argument with a single quote (') from PowerShell, since it reads it as a quote: remove it from the arguments and the checkout path, or run scripts/team.sh in Git Bash"
      }
    }
  }
  # Under the legacy rules the arguments Bash receives can be garbled; refuse them here, before the shim and Bash.
  Assert-NativeArguments -Target "Git Bash" -TargetPath $bashPath -Values (@(Join-Path $PSScriptRoot $ScriptName) + @($ScriptArguments))
  # The helpers call `python3`. On Windows a python.org or winget install has no
  # python3.exe and the Store alias is often a stub, so when Git Bash cannot run
  # python3 a one-file shim for the interpreter found here goes first on PATH for
  # this one run (see resolve_python.ps1).
  $shimDirectory = $null
  $originalPath = $env:PATH
  try {
    if (($PSVersionTable.PSEdition -eq "Desktop") -or ($null -ne $IsWindows -and $IsWindows)) {
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $scratchPath = Join-Path ((Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path) "scratch"
      [System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null
      $shimDirectory = Get-TeamPythonShim -BashPath $bashPath -ScratchRoot $scratchPath
      if ($null -ne $shimDirectory) { $env:PATH = "$shimDirectory;$originalPath" }
    }
    & $bashPath (Join-Path $PSScriptRoot $ScriptName) @ScriptArguments
    $bashStatus = ConvertFrom-GitBashStatus $LASTEXITCODE
  } finally {
    $env:PATH = $originalPath
    if ($null -ne $shimDirectory) { Remove-TeamPythonShim -Directory $shimDirectory }
  }
  if ($bashStatus -ne 0) { Exit-Team $bashStatus }
}

if ([string]::IsNullOrWhiteSpace($Command)) {
  Show-EverydayUsage
  exit 0
}
if ($Command -in @("--help", "-h")) {
  Show-Usage
  exit 0
}

# --schema NAME is the one selection channel: strip it and set PROJECT_SCHEMA so
# every child script's loader narrows to that schema.
if ($Command -notin @("upgrade-template", "verify-local", "template-status", "targets", "migration-plan", "archive-migrations", "dev-test", "skills", "mcp", "feature", "reconcile-migration", "staging-neutralize")) {
  $schemaFiltered = @()
  for ($index = 0; $index -lt $Arguments.Count; $index++) {
    if ($Arguments[$index] -eq "--schema") {
      # An empty name would select nothing, which means every schema.
      if ($index + 1 -ge $Arguments.Count -or [string]::IsNullOrEmpty($Arguments[$index + 1])) { Fail "--schema requires a schema name" }
      $env:PROJECT_SCHEMA = $Arguments[$index + 1]
      $index++
    } elseif ($Arguments[$index] -like "--schema=*") {
      if ($Arguments[$index] -eq "--schema=") { Fail "--schema requires a schema name" }
      $env:PROJECT_SCHEMA = $Arguments[$index].Substring("--schema=".Length)
    } else {
      $schemaFiltered += $Arguments[$index]
    }
  }
  $Arguments = $schemaFiltered
  if (-not [string]::IsNullOrEmpty($env:PROJECT_SCHEMA) -and $env:PROJECT_SCHEMA -cnotmatch '^[A-Z][A-Z0-9_$#]{0,127}$') {
    Fail "--schema must be an uppercase Oracle identifier"
  }
}

try {
  switch ($Command) {
    "doctor" {
      if ($Arguments.Count -ne 0) { Fail "doctor does not accept arguments" }
      . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
      . (Join-Path $PSScriptRoot "invoke_sqlcl.ps1")
      $scratchPath = Join-Path ((Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path) "scratch"
      [System.IO.Directory]::CreateDirectory($scratchPath) | Out-Null
      function Invoke-DoctorOne([string]$Connection, [string]$ExpectedUser, [string]$SchemaName, [string]$DoctorScript = "doctor.sql", [string]$DoctorProfile = "identity") {
        $sqlclWorkDir = Join-Path $scratchPath ("sqlcl-doctor-" + [Guid]::NewGuid().ToString("N"))
        [System.IO.Directory]::CreateDirectory($sqlclWorkDir) | Out-Null
        $stdinFile = Join-Path $sqlclWorkDir ".sqlcl-stdin"
        $transcriptFile = Join-Path $sqlclWorkDir "sqlcl-output.log"
        New-Item -ItemType File -Path $stdinFile | Out-Null
        try {
          $usernameHex = '-'
          if ($DoctorProfile -eq 'apex') {
            if ([string]::IsNullOrWhiteSpace($env:APEX_WORKSPACE_USERNAME)) { throw 'set APEX_WORKSPACE_USERNAME to the existing Builder developer/admin login' }
            $usernameBytes = [System.Text.Encoding]::UTF8.GetBytes($env:APEX_WORKSPACE_USERNAME)
            if ($env:APEX_WORKSPACE_USERNAME -match '[\x00-\x1F]' -or $usernameBytes.Length -gt 255) { throw 'APEX_WORKSPACE_USERNAME must be text without control characters (at most 255 UTF-8 bytes)' }
            $usernameHex = [System.BitConverter]::ToString($usernameBytes).Replace('-', '')
            Assert-SqlclApexVersion -WorkDirectory $sqlclWorkDir
          }
          $sqlclExit = Invoke-Sqlcl -WorkingDirectory $sqlclWorkDir -StdInFile $stdinFile `
            -TranscriptFile $transcriptFile -Arguments @(
            "-S", "-noupdates", "-name", $Connection,
            "@$(Join-Path $PSScriptRoot $DoctorScript)",
            $SchemaName, $env:DB_ENVIRONMENT, $ExpectedUser, $DoctorProfile, $usernameHex, $env:APEX_APP_ID
          )
          $output = [System.IO.File]::ReadAllText($transcriptFile)
          # Write-Host, not Write-Output: this function returns a status, and anything
          # written to the output stream would become part of that return value.
          Write-Host $output
          if ($sqlclExit -ne 0) {
            [Console]::Error.WriteLine("team error: SQLcl doctor check failed for schema $SchemaName (connection $Connection)")
            return $false
          }
          if ([regex]::Matches($output, '(?m)^DATABASE_VERSION_VERIFIED:').Count -ne 1 -or
              $output -notmatch '(?m)^DATABASE_VERSION_VERIFIED:[0-9]+([.][0-9]+)*:[0-9]+([.][0-9]+)*\r?$') {
            [Console]::Error.WriteLine("team error: database version/compatibility detection is unknown for schema $SchemaName")
            return $false
          }
          if ($output -notmatch "(?m)^\s*APEX_DOCTOR_VERIFIED:$([regex]::Escape($ExpectedUser))\s*$") {
            [Console]::Error.WriteLine("team error: SQLcl did not verify the doctor script for schema $SchemaName; the result is unknown")
            return $false
          }
          if ($DoctorProfile -eq "apex" -and $output -notmatch '(?m)^\s*APEX_RELEASE_VERIFIED:26[.]2\s*$') {
            [Console]::Error.WriteLine("team error: APEX release was not verified; the result is unknown")
            return $false
          }
          if ($DoctorProfile -eq 'apex' -and $output -notmatch '(?m)^\s*APEX_WORKSPACE_USERS_VERIFIED\s*$') {
            [Console]::Error.WriteLine('team error: workspace developer validation was not verified; the result is unknown')
            return $false
          }
          return $true
        } catch {
          # Invoke-Sqlcl uses Start-Process, which throws when SQLcl cannot start.
          # That is a failed check, not a reason to skip the remaining schemas.
          [Console]::Error.WriteLine("team error: SQLcl doctor check failed for schema $SchemaName (connection $Connection): $($_.Exception.Message)")
          return $false
        } finally {
          for ($cleanupAttempt = 0; $cleanupAttempt -lt 10; $cleanupAttempt++) {
            if (-not (Test-Path -LiteralPath $sqlclWorkDir)) { break }
            Remove-Item -LiteralPath $sqlclWorkDir -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $sqlclWorkDir)) { break }
            Start-Sleep -Milliseconds 200
          }
        }
      }

      $doctorSeen = @{}
      $doctorTotal = 0
      $doctorFailed = 0
      # ORDS has its own floor; APEX checks its qualified floor separately.
      $doctorOrdsSqlclOk = $true
      if (-not [string]::IsNullOrEmpty($env:ORDS_SCHEMA)) {
        $versionDirectory = Join-Path $scratchPath ("sqlcl-doctor-" + [Guid]::NewGuid().ToString("N"))
        try {
          Assert-SqlclOrdsVersion -WorkDirectory $versionDirectory
        } catch {
          [Console]::Error.WriteLine($_.Exception.Message)
          $doctorOrdsSqlclOk = $false
        } finally {
          Remove-Item -LiteralPath $versionDirectory -Recurse -Force -ErrorAction SilentlyContinue
        }
      }
      foreach ($doctorProfile in @("apex", "tables", "code", "migration", "ords")) {
        $doctorScript = "doctor.sql"
        switch ($doctorProfile) {
          "apex"   { $doctorSchemas = $env:APEX_PARSING_SCHEMA; $doctorConnections = $env:APEX_SQLCL_CONNECTION; $doctorUsers = $env:APEX_EXPECTED_USER }
          "tables" { $doctorSchemas = $env:TABLES_SCHEMA; $doctorConnections = $env:TABLES_SQLCL_CONNECTION; $doctorUsers = $env:TABLES_EXPECTED_USER }
          "code"   { $doctorSchemas = $env:CODE_SCHEMA; $doctorConnections = $env:CODE_SQLCL_CONNECTION; $doctorUsers = $env:CODE_EXPECTED_USER }
          "migration" { $doctorSchemas = $env:MIGRATION_SCHEMA; $doctorConnections = $env:MIGRATION_SQLCL_CONNECTION; $doctorUsers = $env:MIGRATION_EXPECTED_USER }
          "ords"   { $doctorSchemas = $env:ORDS_SCHEMA; $doctorConnections = $env:ORDS_SQLCL_CONNECTION; $doctorUsers = $env:ORDS_EXPECTED_USER; $doctorScript = "doctor_ords.sql" }
        }
        if ([string]::IsNullOrEmpty($doctorSchemas)) { continue }
        $schemaList = @($doctorSchemas.Split(","))
        $connectionList = @($doctorConnections.Split(","))
        $userList = @($doctorUsers.Split(","))
        for ($doctorIndex = 0; $doctorIndex -lt $schemaList.Count; $doctorIndex++) {
          $doctorKey = "$($connectionList[$doctorIndex])|$($userList[$doctorIndex])|$($schemaList[$doctorIndex])"
          # The ORDS check is stricter, so another profile's check never stands in for it.
          if ($doctorProfile -eq "ords") { $doctorKey = "ords|$doctorKey" }
          if ($doctorSeen.ContainsKey($doctorKey)) { continue }
          $doctorSeen[$doctorKey] = $true
          $doctorTotal++
          & (Join-Path $PSScriptRoot "check_db_target.ps1") -Operation read -Target $doctorProfile -Schema $schemaList[$doctorIndex]
          if ($env:PROJECT_MULTI_SCHEMA -eq "true") {
            Write-Output "Doctor: schema $($schemaList[$doctorIndex]) via connection $($connectionList[$doctorIndex]) as $($userList[$doctorIndex])"
          }
          if ($doctorProfile -eq "ords" -and -not $doctorOrdsSqlclOk) { $doctorFailed++; continue }
          if (-not (Invoke-DoctorOne $connectionList[$doctorIndex] $userList[$doctorIndex] $schemaList[$doctorIndex] $doctorScript $doctorProfile)) { $doctorFailed++ }
        }
      }
      if ($doctorTotal -eq 0) { Fail "no configured profile lists schema $($env:PROJECT_SCHEMA)" }
      if ($doctorFailed -gt 0) { Fail "$doctorFailed of $doctorTotal doctor check(s) failed" }
      if ($doctorTotal -eq 1) {
        Write-Output "Doctor checks passed for the configured DEV connection."
      } else {
        Write-Output "Doctor checks passed for all $doctorTotal configured DEV schema connections."
      }
    }
    "export" {
      if ($Arguments.Count -ne 1) { Fail "usage: scripts/team.ps1 export <numeric_app_id>" }
      if ($Arguments[0] -cnotmatch '^[1-9][0-9]{0,17}$') { Fail "expected a positive numeric application id of at most 18 digits" }
      & (Join-Path $PSScriptRoot "export_apps.ps1") -AppId $Arguments[0]
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    "upgrade-apexlang" {
      & (Join-Path $PSScriptRoot 'upgrade_apexlang.ps1') @Arguments
      Exit-Team $LASTEXITCODE
    }
    "publish" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 publish <numeric_app_id> [--env dev] [--force]" }
      for ($index = 0; $index -lt $Arguments.Count; $index++) {
        if ($Arguments[$index] -eq "--env") {
          if ($index + 1 -ge $Arguments.Count) { Fail "--env requires dev; use deploy for staging or production" }
          if ($Arguments[$index + 1] -cne "dev") { Fail "publish targets DEV only; use deploy for staging or production" }
        }
      }
      & (Join-Path $PSScriptRoot "publish_app.ps1") @Arguments
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    { $_ -cin @('app-lock', 'app-unlock') } {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 $Command <numeric_app_id> [--env dev]" }
      $lockOperation = if ($Command -ceq 'app-lock') { 'acquire' } else { 'unlock' }
      & (Join-Path $PSScriptRoot 'app_lock.ps1') $lockOperation @Arguments
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    "check-conflicts" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 check-conflicts <migration-folder> [...] (--env dev|staging|prod | --local)" }
      Invoke-TeamBash -ScriptName "check_conflicts.sh" -ScriptArguments @($Arguments | ForEach-Object { ConvertTo-MigrationFolderArgument $_ })
    }
    "migration-plan" {
      . (Join-Path $PSScriptRoot "migration_plan_args.ps1")
      $planArguments = @()
      foreach ($classifiedArgument in @(Get-MigrationPlanArgumentKinds -Values $Arguments)) {
        if ($classifiedArgument.kind -ceq "folder") {
          $planArguments += ConvertTo-MigrationFolderArgument ([string] $classifiedArgument.value)
        } else {
          $planArguments += [string] $classifiedArgument.value
        }
      }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("migration-plan") + $planArguments)
    }
    "archive-migrations" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 archive-migrations <folder> [...] [--schema SCHEMA] [--destination DIR] [--reason TEXT] [--yes] | --all [--keep <folder>...]" }
      $archiveArguments = @($Arguments)
      for ($archiveIndex = 0; $archiveIndex -lt $archiveArguments.Count; $archiveIndex++) {
        $archiveValue = [string] $archiveArguments[$archiveIndex]
        if ($archiveValue -cin @("--reason", "--schema") -and $archiveIndex + 1 -lt $archiveArguments.Count) {
          $archiveIndex++
        } elseif ($archiveValue -ceq "--keep" -and $archiveIndex + 1 -lt $archiveArguments.Count) {
          $archiveIndex++
          $archiveArguments[$archiveIndex] = ConvertTo-MigrationFolderArgument ([string] $archiveArguments[$archiveIndex])
        } elseif ($archiveValue -ceq "--destination" -and $archiveIndex + 1 -lt $archiveArguments.Count) {
          $archiveIndex++
          $archiveArguments[$archiveIndex] = ConvertTo-MigrationFolderArgument ([string] $archiveArguments[$archiveIndex])
        } elseif ($archiveValue.StartsWith("--keep=", [System.StringComparison]::Ordinal)) {
          $keepValue = $archiveValue.Substring("--keep=".Length)
          $archiveArguments[$archiveIndex] = "--keep=" + (ConvertTo-MigrationFolderArgument $keepValue)
        } elseif ($archiveValue.StartsWith("--destination=", [System.StringComparison]::Ordinal)) {
          $destinationValue = $archiveValue.Substring("--destination=".Length)
          $archiveArguments[$archiveIndex] = "--destination=" + (ConvertTo-MigrationFolderArgument $destinationValue)
        } elseif (-not $archiveValue.StartsWith("-")) {
          $archiveArguments[$archiveIndex] = ConvertTo-MigrationFolderArgument $archiveValue
        }
      }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("archive-migrations") + $archiveArguments)
    }
    "reconcile-migration" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 reconcile-migration <folder> --env dev|staging|prod --outcome <outcome> --evidence <file> [--attempt <id>] [--superseded-by <folder>] [--yes|-y]" }
      $reconcileArguments = @($Arguments)
      for ($reconcileIndex = 0; $reconcileIndex -lt $reconcileArguments.Count; $reconcileIndex++) {
        $reconcileValue = [string] $reconcileArguments[$reconcileIndex]
        if ($reconcileValue -ceq "--evidence" -and $reconcileIndex + 1 -lt $reconcileArguments.Count) {
          $reconcileIndex++
          $reconcileArguments[$reconcileIndex] = ConvertTo-MigrationFolderArgument ([string] $reconcileArguments[$reconcileIndex])
        } elseif ($reconcileValue.StartsWith("--evidence=", [System.StringComparison]::Ordinal)) {
          $evidenceValue = $reconcileValue.Substring("--evidence=".Length)
          $reconcileArguments[$reconcileIndex] = "--evidence=" + (ConvertTo-MigrationFolderArgument $evidenceValue)
        } elseif (-not $reconcileValue.StartsWith("-") -and $reconcileIndex -eq 0) {
          $reconcileArguments[$reconcileIndex] = ConvertTo-MigrationFolderArgument $reconcileValue
        }
      }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("reconcile-migration") + $reconcileArguments)
    }
    "dev-test" {
      Invoke-TeamBash -ScriptName "dev_test.sh" -ScriptArguments $Arguments
    }
    "migrate" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 migrate <migration-folder> [...] --env dev|staging|prod [--verbose] [--rehearse] [--report <file>]" }
      $migrateArguments = @($Arguments)
      for ($migrateIndex = 0; $migrateIndex -lt $migrateArguments.Count; $migrateIndex++) {
        if ($migrateArguments[$migrateIndex] -ceq "--report" -and $migrateIndex + 1 -lt $migrateArguments.Count) {
          $migrateIndex++
          $migrateArguments[$migrateIndex] = ConvertTo-MigrationFolderArgument ([string] $migrateArguments[$migrateIndex])
        } elseif ([string] $migrateArguments[$migrateIndex] -clike "--report=*") {
          $reportValue = ([string] $migrateArguments[$migrateIndex]).Substring("--report=".Length)
          $migrateArguments[$migrateIndex] = "--report=" + (ConvertTo-MigrationFolderArgument $reportValue)
        } elseif (-not ([string] $migrateArguments[$migrateIndex]).StartsWith("-")) {
          $migrateArguments[$migrateIndex] = ConvertTo-MigrationFolderArgument ([string] $migrateArguments[$migrateIndex])
        }
      }
      Invoke-TeamBash -ScriptName "migrate.sh" -ScriptArguments $migrateArguments
    }
    "query" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 query --env dev|staging|prod --connection tables|code|apex|ords|migration [--file <path> | SQL]" }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("query") + @($Arguments))
    }
    "new-migration" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 new-migration <name> --source TYPE:NAME [...] --env dev [--schema NAME]" }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("new-migration") + @($Arguments))
    }
    "revise" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 revise <folder> [--reason TEXT] | revise --check <folder>" }
      $revisionArguments = @($Arguments)
      if ($revisionArguments[0] -ceq "--check") {
        if ($revisionArguments.Count -lt 2) { Fail "usage: scripts/team.ps1 revise --check <folder>" }
        $revisionArguments[1] = ConvertTo-MigrationFolderArgument ([string] $revisionArguments[1])
      } else {
        $revisionArguments[0] = ConvertTo-MigrationFolderArgument ([string] $revisionArguments[0])
      }
      $teamArguments = @("revise") + $revisionArguments
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments $teamArguments
    }
    "verify" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 verify <folder> [...] --env <env> [--phase pre|post|both] [--only-failed] [--format text|json] [--jobs N]" }
      Invoke-TeamBash -ScriptName "verify_checks.sh" -ScriptArguments @($Arguments | ForEach-Object { ConvertTo-MigrationFolderArgument $_ })
    }
    "rollout" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 rollout <manifest.json> --env <env> [--from-step N] [--dry-run] [--prepare] [--capsule DIR] [--stage-report FILE] [--recover ATTEMPT] [--report <file>]" }
      $rolloutArguments = @($Arguments)
      if (-not $rolloutArguments[0].StartsWith("-")) {
        $rolloutArguments[0] = ConvertTo-MigrationFolderArgument ([string] $rolloutArguments[0])
      }
      for ($rolloutIndex = 1; $rolloutIndex -lt $rolloutArguments.Count; $rolloutIndex++) {
        $rolloutValue = [string] $rolloutArguments[$rolloutIndex]
        if ($rolloutValue -cin @("--report", "--capsule", "--stage-report") -and $rolloutIndex + 1 -lt $rolloutArguments.Count) {
          $rolloutIndex++
          $rolloutArguments[$rolloutIndex] = ConvertTo-MigrationFolderArgument ([string] $rolloutArguments[$rolloutIndex])
        } else {
          foreach ($pathOption in @("--report", "--capsule", "--stage-report")) {
            $prefix = $pathOption + "="
            if ($rolloutValue.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
              $rolloutArguments[$rolloutIndex] = $prefix + (ConvertTo-MigrationFolderArgument $rolloutValue.Substring($prefix.Length))
              break
            }
          }
        }
      }
      Invoke-TeamBash -ScriptName "rollout.sh" -ScriptArguments $rolloutArguments
    }
    "feature" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 feature <init|capture|apply|diff|doctor|freeze|clean-capsule|reconcile-run|convert-plan|release-plan> [arguments]" }
      $featureArguments = @($Arguments)
      if ($featureArguments[0] -ceq "freeze" -and $featureArguments.Count -gt 1) {
        $featureArguments[1] = ConvertTo-MigrationFolderArgument ([string] $featureArguments[1])
      }
      if ($featureArguments[0] -ceq "convert-plan") {
        # Only positional arguments are folders; the values of --feature and --format stay opaque.
        $expectsValue = $false
        for ($featureIndex = 1; $featureIndex -lt $featureArguments.Count; $featureIndex++) {
          $featureValue = [string] $featureArguments[$featureIndex]
          if ($expectsValue) { $expectsValue = $false; continue }
          if ($featureValue -ceq "--feature" -or $featureValue -ceq "--format") { $expectsValue = $true; continue }
          if (-not $featureValue.StartsWith("-")) {
            $featureArguments[$featureIndex] = ConvertTo-MigrationFolderArgument $featureValue
          }
        }
      }
      if ($featureArguments[0] -ceq "apply" -or $featureArguments[0] -ceq "reconcile-run") {
        for ($featureIndex = 1; $featureIndex -lt $featureArguments.Count; $featureIndex++) {
          $featureValue = [string] $featureArguments[$featureIndex]
          if ($featureValue -cmatch '^--(?:checks|execute-plan|evidence)$' -and $featureIndex + 1 -lt $featureArguments.Count) {
            $featureIndex++
            $featureArguments[$featureIndex] = ConvertTo-MigrationFolderArgument ([string] $featureArguments[$featureIndex])
          } elseif ($featureValue -cmatch '^(--(?:checks|execute-plan|evidence)=)(.*)$') {
            $featureArguments[$featureIndex] = $Matches[1] + (ConvertTo-MigrationFolderArgument $Matches[2])
          }
        }
      }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("feature") + $featureArguments)
    }
    "compare-schema" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 compare-schema [--from <env>] (--to <env>|--env <env>) (--object <name>|--pattern <glob>) [...]" }
      Invoke-TeamBash -ScriptName "compare_schema.sh" -ScriptArguments $Arguments
    }
    "compare-env" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 compare-env --from <env> --to <env> [--section <name>] [...] [--format text|json|markdown] [--emit-dba-script <file>]" }
      $compareArguments = @($Arguments)
      for ($compareIndex = 0; $compareIndex -lt $compareArguments.Count; $compareIndex++) {
        if ($compareArguments[$compareIndex] -ceq "--emit-dba-script" -and $compareIndex + 1 -lt $compareArguments.Count) {
          $compareIndex++
          $compareArguments[$compareIndex] = ConvertTo-MigrationFolderArgument ([string] $compareArguments[$compareIndex])
        } elseif ([string] $compareArguments[$compareIndex] -clike "--emit-dba-script=*") {
          $scriptValue = ([string] $compareArguments[$compareIndex]).Substring("--emit-dba-script=".Length)
          $compareArguments[$compareIndex] = "--emit-dba-script=" + (ConvertTo-MigrationFolderArgument $scriptValue)
        }
      }
      # Command help is argparse-only and must work without a configured .env, as in team.sh.
      if ($compareArguments -ccontains "--help" -or $compareArguments -ccontains "-h") {
        Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("compare-env") + $compareArguments)
        Exit-Team 0
      }
      Invoke-TeamBash -ScriptName "compare_schema.sh" -ScriptArguments (@("compare-env") + $compareArguments)
    }
    "baseline" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 baseline <export-source|export-grants|export-data|build|filter-ords> [options]" }
      $baselineArguments = @($Arguments)
      for ($baselineIndex = 0; $baselineIndex -lt $baselineArguments.Count; $baselineIndex++) {
        $pathOptions = @("--scratch", "--data-dir", "--input", "--output")
        if ($pathOptions -ccontains ([string] $baselineArguments[$baselineIndex]) -and $baselineIndex + 1 -lt $baselineArguments.Count) {
          $baselineIndex++
          $baselineArguments[$baselineIndex] = ConvertTo-MigrationFolderArgument ([string] $baselineArguments[$baselineIndex])
        } else {
          foreach ($pathOption in $pathOptions) {
            $prefix = $pathOption + "="
            if (([string] $baselineArguments[$baselineIndex]).StartsWith($prefix, [System.StringComparison]::Ordinal)) {
              $pathValue = ([string] $baselineArguments[$baselineIndex]).Substring($prefix.Length)
              $baselineArguments[$baselineIndex] = $prefix + (ConvertTo-MigrationFolderArgument $pathValue)
              break
            }
          }
        }
      }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("baseline") + $baselineArguments)
    }
    "staging-neutralize" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 staging-neutralize --capture|--check|--apply|--restore FILE [--confirm-target <db_unique_name>] [--yes|-y]" }
      Invoke-TeamBash -ScriptName "team.sh" -ScriptArguments (@("staging-neutralize") + @($Arguments))
    }
    "backup-db" {
      if ($Arguments.Count -ne 0) { Fail "backup-db does not accept arguments" }
      & (Join-Path $PSScriptRoot "backup_db.ps1")
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    "backup-ords" {
      if ($Arguments.Count -ne 0) { Fail "backup-ords does not accept arguments" }
      & (Join-Path $PSScriptRoot "backup_db.ps1") -OrdsOnly
      if ($LASTEXITCODE -ne 0) { Exit-Team $LASTEXITCODE }
    }
    "deploy" {
      if ($Arguments.Count -lt 1) { Fail "usage: scripts/team.ps1 deploy <numeric_app_id> --env <staging|prod> [--manual] [--strict-bytes] [--no-restore-automations]" }
      Invoke-TeamBash -ScriptName "deploy.sh" -ScriptArguments $Arguments
    }
    "verify-local" {
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $python = Resolve-TeamPython
      if ($null -eq $python) { Fail "Python 3.10 or newer is required to verify local readiness (python3, python or py -3)" }
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      Assert-NativeArguments -Target "Python" -TargetPath $python.Path -Values @($Arguments)
      & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot "verify_local.py") --repo-root $repoRoot @Arguments
      Exit-Team $LASTEXITCODE
    }
    "mcp" {
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $python = Resolve-TeamPython
      if ($null -eq $python) { Fail "Python 3.10 or newer is required to configure browser MCPs" }
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      Assert-NativeArguments -Target "Python" -TargetPath $python.Path -Values @($Arguments)
      & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot "browser_mcp.py") --repo-root $repoRoot @Arguments
      Exit-Team $LASTEXITCODE
    }
    "skills" {
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $python = Resolve-TeamPython
      if ($null -eq $python) { Fail "Python 3.10 or newer is required to set up workflow skills" }
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      Assert-NativeArguments -Target "Python" -TargetPath $python.Path -Values @($Arguments)
      & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot "workflow_skills.py") --repo-root $repoRoot @Arguments
      Exit-Team $LASTEXITCODE
    }
    "targets" {
      # Offline and read-only: the report reads the loaded .env names and never connects or starts SQLcl.
      # It lists every configured schema, so --schema is refused and an inherited PROJECT_SCHEMA is cleared.
      foreach ($targetsArgument in $Arguments) {
        if ($targetsArgument -eq "--schema" -or $targetsArgument -like "--schema=*") {
          Fail "targets does not accept --schema: it reports every configured schema"
        }
      }
      Remove-Item -LiteralPath Env:PROJECT_SCHEMA -ErrorAction SilentlyContinue
      # Help is argparse-only and must work without a configured .env, as in compare-env.
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      # Resolve a relative PROJECT_ENV_FILE as load_env.ps1 does: the caller's directory first, then the repository root.
      $targetsEnvFile = if ([string]::IsNullOrWhiteSpace($env:PROJECT_ENV_FILE)) { Join-Path $repoRoot ".env" } else { $env:PROJECT_ENV_FILE }
      if (-not [System.IO.Path]::IsPathRooted($targetsEnvFile) -and -not (Test-Path -LiteralPath $targetsEnvFile -PathType Leaf)) {
        $targetsEnvFile = Join-Path $repoRoot $targetsEnvFile
      }
      $targetsHelp = ($Arguments -ccontains "--help" -or $Arguments -ccontains "-h")
      if (-not $targetsHelp -and (Test-Path -LiteralPath $targetsEnvFile -PathType Leaf)) {
        . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $env:PROJECT_ENV_FILE
      }
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $python = Resolve-TeamPython
      if ($null -eq $python) { Fail "Python 3.10 or newer is required to show the configured targets (python3, python or py -3)" }
      Assert-NativeArguments -Target "Python" -TargetPath $python.Path -Values @($Arguments)
      # Extra or unknown arguments are refused by the module's parser (exit 2), as template-status refuses them.
      Push-Location -LiteralPath $repoRoot
      try {
        if ($targetsHelp -or (Test-Path -LiteralPath $targetsEnvFile -PathType Leaf)) {
          # The prog name is single-quoted inside the Python source: Windows PowerShell drops embedded double quotes from native arguments.
          & $python.Path @($python.Prefix) -c 'import sys; from scripts.show_targets import main; raise SystemExit(main(sys.argv[1:], environ=None, prog=''scripts/team.ps1 targets''))' @Arguments
        } else {
          # No .env is not an error here, as for template-status: every row reports "not configured". The module reads
          # the process environment, so an empty mapping stops a shell variable such as CODE_SCHEMA from appearing as configuration.
          & $python.Path @($python.Prefix) -c 'import sys; from scripts.show_targets import main; raise SystemExit(main(sys.argv[1:], environ={}, prog=''scripts/team.ps1 targets''))' @Arguments
        }
        $targetsStatus = $LASTEXITCODE
      } finally {
        Pop-Location
      }
      Exit-Team $targetsStatus
    }
    "template-status" {
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $python = Resolve-TeamPython
      if ($null -eq $python) { Fail "Python 3.10 or newer is required to inspect template status" }
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      Assert-NativeArguments -Target "Python" -TargetPath $python.Path -Values @($Arguments)
      & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot "template_drift.py") --repo-root $repoRoot @Arguments
      Exit-Team $LASTEXITCODE
    }
    "automations" {
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $python = Resolve-TeamPython
      if ($null -eq $python) { Fail "Python 3.10 or newer is required to operate on APEX automations" }
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      Assert-NativeArguments -Target "Python" -TargetPath $python.Path -Values @($Arguments)
      & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot "automation_operations.py") --repo-root $repoRoot @Arguments
      Exit-Team $LASTEXITCODE
    }
    "upgrade-template" {
      . (Join-Path $PSScriptRoot "resolve_python.ps1")
      $python = Resolve-TeamPython
      if ($null -eq $python) { Fail "Python 3.10 or newer is required to upgrade the template (python3, python or py -3)" }
      $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
      Assert-NativeArguments -Target "Python" -TargetPath $python.Path -Values @($Arguments)
      & $python.Path @($python.Prefix) (Join-Path $PSScriptRoot "upgrade_template.py") --project-root $repoRoot @Arguments
      $upgradeStatus = $LASTEXITCODE
      $envFile = Join-Path $repoRoot ".env"
      if ($upgradeStatus -ne 2 -and $Arguments -notcontains "--dry-run" -and (Test-Path -LiteralPath $envFile)) {
        try {
          . (Join-Path $PSScriptRoot "load_env.ps1") -EnvFile $envFile
        } catch {
          [Console]::Error.WriteLine("WARNING: " + ".env needs attention after the upgrade: $($_.Exception.Message)")
        }
      }
      Exit-Team $upgradeStatus
    }
    default { Fail "unknown command '$Command'; use --help to list commands" }
  }
  $finished = $true
} catch {
  $finished = $true
  # A helper script refuses by throwing a string, which pwsh would print as an
  # exception block (script path, line, caret) and turn into status 1. Bash prints
  # the message alone and exits 2, or 1 for a bad .env: do the same. Any other
  # error is a bug or the platform failing; it is rethrown so its position stays.
  if ($_.Exception -is [System.Management.Automation.RuntimeException] -and $_.FullyQualifiedErrorId -ceq $_.Exception.Message) {
    $refusal = $_.Exception.Message
    $refusalStatus = 2
    if ([System.IO.Path]::GetFileName([string] $_.InvocationInfo.ScriptName) -eq "load_env.ps1") {
      # Bash's loader puts this prefix on every message; the PowerShell one does not.
      if (-not $refusal.StartsWith("project environment error: ")) { $refusal = "project environment error: $refusal" }
      $refusalStatus = 1
    }
    [Console]::Error.WriteLine($refusal)
    Restore-CallerProjectSchema
    exit $refusalStatus
  }
  throw
} finally {
  # Every command ends here, including the ones that exit early through Exit-Team or Fail:
  # the caller's PROJECT_SCHEMA is put back before the script's status is reported.
  Restore-CallerProjectSchema
  # Ctrl-C stops this script as well as the helper it was running, so nothing
  # after the helper runs and pwsh would exit 0, which a caller reads as success.
  # It does not run the catch block, so a script that ended without finishing was
  # interrupted. Report what Bash would: the status the helper chose if it had
  # already ended (migrate says 2 for "may be partially applied"), else 130.
  # (Only a script without [Parameter()] attributes keeps an exit status chosen
  # in finally; see the top of the file.)
  if (-not $finished) {
    if ($LASTEXITCODE -gt 0) { exit (ConvertFrom-GitBashStatus $LASTEXITCODE) }
    exit 130
  }
}
