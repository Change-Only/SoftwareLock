#=====================================================================
#  软件锁 · 自签名代码签名证书 安装脚本
#
#  作用：生成一张自签名代码签名证书，装入「当前用户」的受信任根证书
#        颁发机构，使 Windows UAC 提权对话框里的发布者从「未知发布者」
#        变为证书主题（SoftwareLock），并消除「此文件没有包含有效的
#        数字签名以验证其发布者」的提示。
#
#  范围：仅写入 CurrentUser 存储（My / Root），不碰 LocalMachine，
#        不需要管理员权限，不影响其他 Windows 账户。
#
#  产出：本脚本可重复执行（幂等）。证书已存在时直接复用。
#        私钥备份与密码写在 build\certs\ 下（该目录已被 .gitignore 排除）。
#
#  用法：powershell -NoProfile -ExecutionPolicy Bypass -File tools\selfsign_cert.ps1
# =====================================================================

$ErrorActionPreference = 'Stop'

$SubjectCN = 'SoftwareLock'
$Subject   = "CN=$SubjectCN"
$OutDir    = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\build\certs'))
$CerPath   = Join-Path $OutDir 'SoftwareLock.cer'
$PfxPath   = Join-Path $OutDir 'SoftwareLock.pfx'
$PwdPath   = Join-Path $OutDir 'pfx_password.txt'
$LogPath   = Join-Path $OutDir 'selfsign.log'

function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Host $line
    if ($script:LogReady) { [IO.File]::AppendAllText($LogPath, $line + "`r`n") }
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
Remove-Item $LogPath -Force -ErrorAction SilentlyContinue
$script:LogReady = $true

Log "=== 自签名代码签名证书安装开始 ==="
Log "脚本目录: $PSScriptRoot"
Log "产出目录: $OutDir"

# ---------- 1. 生成或复用证书 ----------
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -eq $Subject } | Select-Object -First 1

if ($cert) {
    Log "STEP1 复用已有证书 thumbprint=$($cert.Thumbprint) 到期=$($cert.NotAfter -f 'yyyy-MM-dd')"
} else {
    Log "STEP1 未找到证书，开始生成 ..."
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $Subject `
        -FriendlyName 'SoftwareLock 自签名代码签名证书' `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -NotAfter (Get-Date).AddYears(10) `
        -KeyAlgorithm RSA `
        -KeyLength 3072 `
        -HashAlgorithm SHA256 `
        -KeyUsage DigitalSignature
    Log "STEP1 生成完成 thumbprint=$($cert.Thumbprint) 到期=$($cert.NotAfter -f 'yyyy-MM-dd')"
}

# ---------- 2. pfx 密码（首次生成后固定复用） ----------
if (-not (Test-Path $PwdPath)) {
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    [IO.File]::WriteAllText($PwdPath, [Convert]::ToBase64String($bytes))
    Log "STEP2 已生成 pfx 密码文件 $PwdPath"
}
$pwdPlain = ([IO.File]::ReadAllText($PwdPath)).Trim()
$secure   = ConvertTo-SecureString -String $pwdPlain -AsPlainText -Force

# ---------- 3. 导出公钥证书与私钥备份 ----------
$der = $cert.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert)
[IO.File]::WriteAllBytes($CerPath, $der)
Log "STEP3 已导出公钥证书 $CerPath ($($der.Length) 字节)"

Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $secure -Force | Out-Null
Log "STEP3 已导出私钥备份 $PfxPath"

# ---------- 4. 装入当前用户的受信任根 ----------
Log "STEP4 写入 CurrentUser\Root（Windows 会弹出「安全警告」对话框，需点「是」放行；脚本会阻塞等待）..."
$inRoot = Get-ChildItem Cert:\CurrentUser\Root -ErrorAction SilentlyContinue |
          Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
if ($inRoot) {
    Log "STEP4 受信任根中已存在，跳过"
    $script:RootAdded = 'already'
} else {
    $store = New-Object Security.Cryptography.X509Certificates.X509Store('Root', 'CurrentUser')
    $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $store.Add($cert)
    $store.Close()
    Log "STEP4 写入完成，未出现阻塞"
    $script:RootAdded = 'added'
}

# ---------- 5. 用临时文件验证签名链 ----------
$probe = Join-Path $env:TEMP ('sl-sign-probe-' + [Guid]::NewGuid().ToString('N') + '.ps1')
[IO.File]::WriteAllText($probe, 'Write-Host "signature probe"')
try {
    Set-AuthenticodeSignature -FilePath $probe -Certificate $cert -HashAlgorithm SHA256 | Out-Null
    $st = Get-AuthenticodeSignature $probe
    Log "STEP5 探针签名状态 = $($st.Status)"
    if ($st.Status -eq 'Valid') {
        Log "STEP5 结论：签名链校验通过 —— UAC 将显示发布者为「$SubjectCN」"
    } else {
        Log "STEP5 结论：签名链未通过（$($st.Status)）—— 受信任根可能尚未生效"
    }
} finally {
    Remove-Item $probe -Force -ErrorAction SilentlyContinue
}

Log "=== 完成 ==="
