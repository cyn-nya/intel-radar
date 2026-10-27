# 情报雷达 · 注册 Windows 任务计划程序
# ---------------------------------------------------------------
# 目的：让采集在看板没开着、甚至笔记本合盖的情况下也能按时跑。
#
# 为什么不用 schtasks /XML：任务 XML 的编码要求很别扭（要 UTF-16），
# 而 PowerShell 的 Register-ScheduledTask 直接给强类型参数，不会踩编码坑。
#
# 关键设置 StartWhenAvailable：到点时电脑在关机/休眠，Windows 会在
# 电脑恢复可用后尽快补跑一次 —— 这正是笔记本最需要的（而不是让看板常驻）。
#
# 本脚本不做任何删除操作；卸载请用 卸载定时任务.bat。

param(
    [string]$TaskPrefix = "IntelRadar"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RadarDir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RadarDir "run.py"))) {
    Write-Host "  [X] 找不到 run.py，脚本位置不对：$RadarDir"
    exit 1
}

$PythonW = Join-Path $RadarDir ".venv\Scripts\pythonw.exe"
$Python  = Join-Path $RadarDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonW)) {
    Write-Host "  [X] 找不到 $PythonW"
    Write-Host "      请先建好虚拟环境并安装依赖（见 README）。"
    exit 1
}

# --- 从 config.yaml 读时刻，而不是在脚本里再抄一份 ---
$TimeHelper = Join-Path $PSScriptRoot "print_times.py"
$timesRaw = ""
if (Test-Path $TimeHelper) {
    $timesRaw = & $Python $TimeHelper 2>$null
}
$Times = @($timesRaw -split '\s+' | Where-Object { $_ -match '^\d{1,2}:\d{2}$' })
if ($Times.Count -eq 0) {
    Write-Host "  [!] 没能从 config.yaml 读到 schedule.times，回退到 09:10 / 21:10"
    $Times = @("09:10", "21:10")
}

Write-Host ""
Write-Host "  雷达目录 : $RadarDir"
Write-Host "  解释器   : $PythonW  (无窗口)"
Write-Host "  采集时刻 : $($Times -join '、')"
Write-Host ""

# --- 公共设置 ---
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 0
# 说明：这里**允许**用电池启动，因为"要不要跑"交给雷达自己的电源守卫判断
# （config.yaml 里的 schedule.power_guard，默认要求插电且电量 >= 35%）。
# 两处都拦的话，排障时会分不清是谁拦的。

$Registered = @()
foreach ($t in $Times) {
    $name = "$TaskPrefix-" + ($t -replace ':', '')
    $action = New-ScheduledTaskAction `
        -Execute $PythonW `
        -Argument "run.py auto" `
        -WorkingDirectory $RadarDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $t
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    try {
        Register-ScheduledTask `
            -TaskName $name -Action $action -Trigger $trigger `
            -Settings $Settings -Principal $principal `
            -Description "情报雷达：每天 $t 采集一次开源项目情报（电源守卫在 radar/config.yaml 里控制）" `
            -Force | Out-Null
        Write-Host "  [OK] 已注册 $name  ->  每天 $t"
        $Registered += $name
    } catch {
        Write-Host "  [X] 注册 $name 失败: $($_.Exception.Message)"
    }
}

if ($Registered.Count -eq 0) {
    Write-Host ""
    Write-Host "  没有注册成功任何任务。"
    exit 1
}

Write-Host ""
Write-Host "  完成。共 $($Registered.Count) 个任务：$($Registered -join '、')"
Write-Host ""
Write-Host "  接下来："
Write-Host "    * 查看  : schtasks /Query /TN $($Registered[0]) /V /FO LIST"
Write-Host "    * 立即测: schtasks /Run   /TN $($Registered[0])"
Write-Host "    * 取消  : 双击 卸载定时任务.bat"
Write-Host ""
Write-Host "  电脑到点时若在关机/休眠，恢复后会由 StartWhenAvailable 自动补跑。"
Write-Host "  日志在 data\radar.log。"
Write-Host ""
