param(
    [string]$RuleName = 'KEEPPER Clipwatch Push 8900'
)
# Prints the effective scope of the inbound push rule so you can verify that the
# peer really can reach this machine's receiver. Run from an ELEVATED PowerShell
# (reading firewall rules is admin-only, same as creating them).

$ErrorActionPreference = 'Stop'

$r = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction Stop
$a = $r | Get-NetFirewallAddressFilter
$f = $r | Get-NetFirewallPortFilter
$i = $r | Get-NetFirewallInterfaceTypeFilter

@(
    "display=$($r.DisplayName)"
    "enabled=$($r.Enabled) direction=$($r.Direction) action=$($r.Action) profile=$($r.Profile)"
    "protocol=$($f.Protocol) localport=$($f.LocalPort)"
    "remoteaddress=$($a.RemoteAddress)"
    "interfacetype=$($i.InterfaceType)"
) | ForEach-Object { Write-Host $_ }