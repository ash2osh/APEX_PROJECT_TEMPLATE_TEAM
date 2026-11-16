# Reads the APEX release of this template: apexRelease in template-manifest.json, where a manifest
# without the key is the 26.1 template. The same rule as scripts/template_release.py. Dot-source this file;
# Get-TemplateApexRelease throws when the manifest cannot be read or names an unknown release.
function Get-TemplateApexRelease {
  param([Parameter(Mandatory = $true)][string] $RepoRoot)
  $manifest = Join-Path $RepoRoot "template-manifest.json"
  if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "template manifest not found: $manifest" }
  try {
    $manifestData = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
  } catch {
    throw "cannot read $manifest"
  }
  if ($manifestData.PSObject.Properties.Name -notcontains "apexRelease") { return "26.1" }
  $release = $manifestData.apexRelease
  if (($release -isnot [string]) -or ($release -cnotin @("26.1", "26.2"))) {
    throw "cannot read apexRelease (26.1 or 26.2) from $manifest"
  }
  return $release
}
