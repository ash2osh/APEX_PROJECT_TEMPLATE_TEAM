# Classify migration-plan arguments so only positional folders receive native
# Windows path normalization. Option values are opaque CLI values, even when
# they contain slashes or look like paths.
function Get-MigrationPlanArgumentKinds {
  param([string[]] $Values)

  $valueOptions = @("--env", "--schema", "--format", "--repo-root")
  $items = [System.Collections.Generic.List[object]]::new()
  $expectsValue = $false
  foreach ($value in $Values) {
    if ($expectsValue -and -not $value.StartsWith("-", [System.StringComparison]::Ordinal)) {
      $items.Add([pscustomobject] @{ kind = "option-value"; value = $value })
      $expectsValue = $false
      continue
    }
    $expectsValue = $false
    if ($valueOptions -ccontains $value) {
      $items.Add([pscustomobject] @{ kind = "option"; value = $value })
      $expectsValue = $true
      continue
    }
    if ($value -cmatch '^--(?:env|schema|format|repo-root)=') {
      $items.Add([pscustomobject] @{ kind = "option"; value = $value })
      continue
    }
    if ($value.StartsWith("-", [System.StringComparison]::Ordinal)) {
      $items.Add([pscustomobject] @{ kind = "option"; value = $value })
      continue
    }
    $items.Add([pscustomobject] @{ kind = "folder"; value = $value })
  }
  return $items.ToArray()
}
