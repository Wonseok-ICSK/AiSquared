param(
  [ValidateSet('Set', 'Get')][string]$Action = 'Set',
  [string]$VaultPath = (Join-Path $PSScriptRoot '..\.secrets\integrations.json')
)
$ErrorActionPreference = 'Stop'
# Pin the built-in Windows PowerShell module when launched from a PowerShell 7 host.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$integrationSecrets = @{}
if (Test-Path -LiteralPath $VaultPath) {
  $storedSecrets = Get-Content -LiteralPath $VaultPath -Raw | ConvertFrom-Json
  foreach ($property in $storedSecrets.PSObject.Properties) { $integrationSecrets[$property.Name] = $property.Value }
}
$secretNames = @('AMADEUS_CLIENT_ID', 'AMADEUS_CLIENT_SECRET', 'OPENAI_API_KEY', 'GOOGLE_PLACES_API_KEY')
if ($Action -eq 'Set') {
  Write-Host 'Secrets are encrypted with Windows DPAPI for this Windows account.'
  Write-Host 'Press Enter to keep an existing value or skip an integration.'
  foreach ($secretName in $secretNames) {
    $secureValue = Read-Host $secretName -AsSecureString
    if ($secureValue.Length -gt 0) { $integrationSecrets[$secretName] = ConvertFrom-SecureString -SecureString $secureValue }
    $secureValue.Dispose()
  }
  $vaultDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($VaultPath))
  New-Item -ItemType Directory -Force -Path $vaultDirectory | Out-Null
  $integrationSecrets | ConvertTo-Json | Set-Content -LiteralPath $VaultPath -Encoding UTF8
  Write-Host 'Encrypted integration settings saved. Restart Wayfinder to use them.'
} else {
  # Internal pipe to the Node process. Do not call Get interactively or log its output.
  $plainSecrets = @{}
  foreach ($secretName in $secretNames) {
    if ($integrationSecrets.ContainsKey($secretName)) {
      $secureValue = ConvertTo-SecureString -String $integrationSecrets[$secretName]
      $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
      try { $plainSecrets[$secretName] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer) }
      finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer); $secureValue.Dispose() }
    }
  }
  $plainSecrets | ConvertTo-Json -Compress
}
