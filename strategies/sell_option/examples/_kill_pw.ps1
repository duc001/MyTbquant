$ErrorActionPreference = 'SilentlyContinue'
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*playwright*install*') -or
    ($_.Name -eq 'node.exe' -and $_.CommandLine -like '*playwright*')
}
if (-not $procs) {
    Write-Output 'NO_PLAYWRIGHT_INSTALL_PROC'
} else {
    foreach ($p in $procs) {
        Write-Output ("KILL {0} PID={1}" -f $p.Name, $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
    }
}
