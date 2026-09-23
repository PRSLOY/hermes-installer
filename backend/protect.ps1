param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = 'Stop'
try {
    $item = Get-Item -LiteralPath $Path
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = if ($item.PSIsContainer) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($item.PSIsContainer) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inherit, 'None', 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
    $actual = Get-Acl -LiteralPath $Path
    if (-not $actual.AreAccessRulesProtected) { exit 1 }
    exit 0
} catch { exit 1 }
