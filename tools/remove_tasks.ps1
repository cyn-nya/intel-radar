# 情报雷达 · 卸载 Windows 任务计划程序
# ---------------------------------------------------------------
# 只删除本工具注册的任务（名字以 IntelRadar 开头），不碰其它任何计划任务。

param(
    [string]$TaskPrefix = "IntelRadar"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$tasks = Get-ScheduledTask -TaskName "$TaskPrefix-*" -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host ""
    Write-Host "  没有找到以 $TaskPrefix 开头的计划任务，无需清理。"
    Write-Host ""
    exit 0
}

Write-Host ""
foreach ($t in $tasks) {
    try {
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "  [OK] 已删除 $($t.TaskName)"
    } catch {
        Write-Host "  [X] 删除 $($t.TaskName) 失败: $($_.Exception.Message)"
    }
}
Write-Host ""
Write-Host "  完成。看板和手动双击的脚本不受影响。"
Write-Host ""
