# Creates a MultiMic shortcut on your Desktop. Run once:  right-click > Run with PowerShell
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "MultiMic.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
$s.Arguments = '"' + (Join-Path $dir "MultiMic.vbs") + '"'
$s.WorkingDirectory = $dir
$s.IconLocation = (Join-Path $dir "multimic.ico") + ",0"
$s.Description = "MultiMic - record several USB microphones at once"
$s.Save()
Write-Host "Shortcut created: $lnk"
