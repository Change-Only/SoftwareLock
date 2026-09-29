# 软件锁 · 产物代码签名脚本
#
# 用 CurrentUser\My 中的 CN=SoftwareLock 自签名证书，给打包产物签 Authenticode 签名。
# 签名后 Windows UAC 提权对话框会显示发布者「SoftwareLock」，而不是「未知发布者」。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\sign_artifacts.ps1
#       -> 自动签名 dist 下的单文件版 exe 与便携版目录内的 exe
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\sign_artifacts.ps1 -Path a.exe,b.exe
#       -> 只签指定文件
#
# 注意：目标 exe 若正在运行，签名会失败，请先退出。

param(
    [string[]]$Path
)

$ErrorActionPreference = 'Stop'

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

# ---------- 收集待签名文件 ----------
if (-not $Path -or $Path.Count -eq 0) {
    $dist = Join-Path $Root 'dist'
    $files = New-Object System.Collections.ArrayList
    foreach ($f in (Get-ChildItem -Path $dist -Filter '*.exe' -File -ErrorAction SilentlyContinue)) {
        [void]$files.Add($f.FullName)
    }
    foreach ($d in (Get-ChildItem -Path $dist -Directory -Filter '*便携版*' -ErrorAction SilentlyContinue)) {
        foreach ($f in (Get-ChildItem -Path $d.FullName -Filter '*.exe' -File -Recurse -ErrorAction SilentlyContinue)) {
            [void]$files.Add($f.FullName)
        }
    }
    $Path = $files.ToArray()
}

if (-not $Path -or $Path.Count -eq 0) {
    Write-Host "! 未找到待签名的 exe" -ForegroundColor Yellow
    exit 0
}

# ---------- 找证书 ----------
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -eq 'CN=SoftwareLock' } |
        Select-Object -First 1

if (-not $cert) {
    Write-Host "! 未找到签名证书 CN=SoftwareLock" -ForegroundColor Red
    Write-Host "  请先运行: tools\selfsign_cert.ps1" -ForegroundColor Red
    exit 2
}
Write-Host "证书: $($cert.Subject)  thumbprint=$($cert.Thumbprint)"
Write-Host ""

# ---------- 逐个签名 ----------
$failed = 0
foreach ($f in $Path) {
    if (-not (Test-Path -LiteralPath $f)) {
        Write-Host "跳过（文件不存在）: $f" -ForegroundColor DarkGray
        continue
    }
    try {
        Set-AuthenticodeSignature -FilePath $f -Certificate $cert -HashAlgorithm SHA256 | Out-Null
        $st   = (Get-AuthenticodeSignature -FilePath $f).Status
        $size = [math]::Round((Get-Item -LiteralPath $f).Length / 1MB, 1)
        if ($st -eq 'Valid') {
            Write-Host "OK   $f  ($size MB)  状态=$st" -ForegroundColor Green
        } else {
            Write-Host "警告 $f  状态=$st" -ForegroundColor Yellow
            $failed++
        }
    } catch {
        Write-Host "失败 $f" -ForegroundColor Red
        Write-Host "     $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
if ($failed -gt 0) {
    Write-Host "有 $failed 个文件签名未通过" -ForegroundColor Yellow
    exit 1
}
Write-Host "全部签名完成" -ForegroundColor Green
exit 0
