#Requires -Version 5.1
<#
.SYNOPSIS
    MarkID.exe 에 자체서명(Authenticode) 코드 서명을 적용합니다.

.DESCRIPTION
    - Cert:\CurrentUser\My 에서 게시자(김권수 / HRteam)용 코드 서명 인증서를
      찾고, 없으면 5년 유효한 자체서명 인증서를 새로 만듭니다(멱등).
    - dist\MarkID.exe(또는 -ExePath) 에 SHA-256 Authenticode 서명을 적용하고,
      DigiCert -> Sectigo 순으로 타임스탬프를 시도합니다(둘 다 실패 시 경고 후
      타임스탬프 없이 서명).
    - 서명 결과를 출력하고, 공개 인증서를 dist\ 에 .cer 로 내보냅니다.
    - 신뢰 저장소에는 자동 등록하지 않습니다(배포 PC 신뢰 등록은 README 참고).

.PARAMETER ExePath
    서명할 실행 파일 경로. 기본값: 저장소 루트의 dist\MarkID.exe.

.NOTES
    * 이 스크립트 파일은 UTF-8(BOM) 로 저장해야 Windows PowerShell 5.1 에서
      한글(김권수)이 깨지지 않습니다.
    * 자체서명 인증서는 공인 인증서가 아니므로 다른 PC 에서 SmartScreen 경고가
      날 수 있습니다(README '신뢰 및 서명' 참고).
#>
[CmdletBinding()]
param(
    [string]$ExePath = 'dist\MarkID.exe'
)

$ErrorActionPreference = 'Stop'

# --- 상수 -----------------------------------------------------------------
$CODESIGN_EKU  = '1.3.6.1.5.5.7.3.3'                 # Code Signing EKU OID
$NEW_SUBJECT   = 'CN=김권수, O=HRteam, OU=HRteam'    # 생성할 인증서 주체
$TS_SERVERS    = @('http://timestamp.digicert.com', 'http://timestamp.sectigo.com')

function Write-Info { param([string]$Message) Write-Host "[MarkID 서명] $Message" }
function Write-Warn { param([string]$Message) Write-Host "[MarkID 서명] 경고: $Message" -ForegroundColor Yellow }
function Fail {
    param([string]$Message)
    Write-Host "[MarkID 서명] 오류: $Message" -ForegroundColor Red
    exit 1
}

try {
    # 경로 정규화: 상대 경로는 저장소 루트(tools\ 의 상위) 기준으로 해석
    $RepoRoot = Split-Path -Parent $PSScriptRoot
    if (-not [System.IO.Path]::IsPathRooted($ExePath)) {
        $ExePath = Join-Path $RepoRoot $ExePath
    }

    # ---------------------------------------------------------------------
    # 1) 코드 서명 인증서 확인 / 생성 (멱등)
    # ---------------------------------------------------------------------
    Write-Info '코드 서명 인증서를 확인합니다...'
    $now = Get-Date
    $cert = Get-ChildItem -Path Cert:\CurrentUser\My | Where-Object {
        $_.HasPrivateKey -and
        $_.NotBefore -le $now -and $_.NotAfter -gt $now -and
        $_.Subject -match 'CN=김권수' -and $_.Subject -match 'O=HRteam' -and
        ($_.EnhancedKeyUsageList | Where-Object { $_.ObjectId -eq $CODESIGN_EKU })
    } | Sort-Object NotAfter -Descending | Select-Object -First 1

    if ($null -eq $cert) {
        Write-Info '유효한 인증서가 없어 새 자체서명 인증서를 생성합니다...'
        $cert = New-SelfSignedCertificate -Type CodeSigningCert `
            -Subject $NEW_SUBJECT `
            -CertStoreLocation Cert:\CurrentUser\My `
            -NotAfter (Get-Date).AddYears(5)
        Write-Info "인증서 생성 완료 - 주체: $($cert.Subject)"
        Write-Info "                지문: $($cert.Thumbprint)"
        Write-Info "                유효기간: $($cert.NotBefore) ~ $($cert.NotAfter)"
    }
    else {
        Write-Info "기존 인증서를 사용합니다 - 주체: $($cert.Subject)"
        Write-Info "                          지문: $($cert.Thumbprint)"
    }

    # ---------------------------------------------------------------------
    # 2) 실행 파일 존재 확인 후 서명 + 타임스탬프
    # ---------------------------------------------------------------------
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        Fail "서명할 실행 파일을 찾을 수 없습니다: $ExePath  (먼저 build.bat 으로 빌드하세요.)"
    }
    Write-Info "서명 대상: $ExePath"

    $signed  = $false
    $usedTs  = $null
    foreach ($ts in $TS_SERVERS) {
        try {
            Write-Info "서명 및 타임스탬프 적용 시도 (서버: $ts)..."
            $null = Set-AuthenticodeSignature -FilePath $ExePath -Certificate $cert `
                -HashAlgorithm SHA256 -TimestampServer $ts -ErrorAction Stop
            $signed = $true
            $usedTs = $ts
            Write-Info "타임스탬프 적용 성공 (서버: $ts)"
            break
        }
        catch {
            Write-Warn "타임스탬프 서버 실패 ($ts): $($_.Exception.Message)"
        }
    }

    if (-not $signed) {
        Write-Warn '모든 타임스탬프 서버에 실패하여 타임스탬프 없이 서명합니다. (인증서 만료 후 서명이 무효가 될 수 있습니다.)'
        $null = Set-AuthenticodeSignature -FilePath $ExePath -Certificate $cert `
            -HashAlgorithm SHA256 -ErrorAction Stop
    }

    # ---------------------------------------------------------------------
    # 3) 서명 결과 검증 및 출력
    # ---------------------------------------------------------------------
    $verify = Get-AuthenticodeSignature -FilePath $ExePath
    if ($null -eq $verify.SignerCertificate -or $verify.Status -eq 'NotSigned') {
        Fail "서명이 적용되지 않았습니다 (Status: $($verify.Status))."
    }

    Write-Info '----- 서명 결과 -----'
    Write-Info "Status     : $($verify.Status)"
    Write-Info "Subject    : $($verify.SignerCertificate.Subject)"
    Write-Info "Thumbprint : $($verify.SignerCertificate.Thumbprint)"
    if ($null -ne $verify.TimeStamperCertificate) {
        Write-Info "TimeStamp  : 적용됨 ($usedTs)"
    }
    else {
        Write-Info 'TimeStamp  : 없음'
    }
    if ($verify.Status -eq 'UnknownError') {
        Write-Info '참고: 자체서명 인증서가 아직 신뢰 저장소에 없어 Status 가 UnknownError 로 표시되는 것은 정상입니다.'
        Write-Info '      배포 PC 에서 아래 .cer 를 신뢰할 수 있는 게시자/루트로 등록하면 유효(Valid)로 바뀝니다.'
    }

    # ---------------------------------------------------------------------
    # 4) 공개 인증서 내보내기 (신뢰 저장소 자동 등록은 하지 않음)
    # ---------------------------------------------------------------------
    $cerPath = Join-Path (Split-Path -Parent $ExePath) 'HRteam-김권수-codesign.cer'
    $null = Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT
    Write-Info "공개 인증서 내보내기 완료: $cerPath"
    Write-Info '(이 인증서는 신뢰 저장소에 자동 등록하지 않습니다. 배포 PC 신뢰 등록 방법은 README 를 참고하세요.)'

    Write-Info '완료되었습니다.'
    exit 0
}
catch {
    Fail $_.Exception.Message
}
