param(
    [int]$Port = 8900,
    [string]$RuleName = '',
    [string]$Profile = 'Private'
)
# Adds the inbound rule that lets the peer device POST files / clipboard directly
# to this machine's clipwatch push receiver (protocol v2 direct push).
#
# Both this script and verify_firewall_rule.ps1 MUST run from an ELEVATED PowerShell:
# creating firewall rules needs admin, and reading them does too
# (non-admin gets "Access is denied", System Error 5).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\add_firewall_rule.ps1
#
# Windows blocks inbound by default (DefaultInboundAction = NotConfigured == Block),
# so without this rule the peer's direct push is refused and it silently degrades
# to the v1 queue.

$ErrorActionPreference = 'Stop'
if (-not $RuleName) { $RuleName = "KEEPPER Clipwatch Push $Port" }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'ERROR: run this script from an elevated PowerShell.'
    exit 1
}

$old = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($old) {
    $old | Remove-NetFirewallRule -ErrorAction Stop
    $note = 'replaced existing rule'
} else {
    $note = 'created'
}

$r = New-NetFirewallRule -DisplayName $RuleName `
    -Description 'KEEPPER cross-device file/clipboard direct push receiver (clipwatch.py)' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -RemoteAddress LocalSubnet -Profile $Profile -Enabled True -ErrorAction Stop

$f = $r | Get-NetFirewallPortFilter
Write-Host ('OK {0}; display={1} enabled={2} profile={3} action={4} port={5} remote={6}' -f `
    $note, $r.DisplayName, $r.Enabled, $r.Profile, $r.Action, $f.LocalPort, $f.RemoteAddress)