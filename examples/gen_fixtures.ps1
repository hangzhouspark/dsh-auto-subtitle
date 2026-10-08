# 用 Windows SAPI 把同目录的 *.txt 念成 wav，作为字幕技能的固定回归素材。
# 产物写到 .\audio\；跑批产物建议写到 .\out\ —— 清理时只动 out\，不会误删素材。
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File gen_fixtures.ps1
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$fix   = Split-Path -Parent $MyInvocation.MyCommand.Path
$audio = Join-Path $fix "audio"
New-Item -ItemType Directory -Force -Path $audio | Out-Null

# 中文语音（Microsoft Huihui Desktop）随中文版 Windows 提供；
# 只有英文版系统的话，把 "zh" 换成 "Microsoft Zira Desktop" 也能跑通流程，
# 只是 zh.wav 里念的是英文，期望值要按英文那套看。
$map = [ordered]@{
    "zh" = "Microsoft Huihui Desktop"
    "en" = "Microsoft Zira Desktop"
}

$installed = (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() |
             ForEach-Object { $_.VoiceInfo.Name }

foreach ($k in $map.Keys) {
    $voice = $map[$k]
    if ($installed -notcontains $voice) {
        Write-Warning "找不到语音 '$voice'，跳过 $k.txt。已安装的语音: $($installed -join ', ')"
        continue
    }
    $txt = (Get-Content (Join-Path $fix "$k.txt") -Raw -Encoding UTF8).Trim()
    $wav = Join-Path $audio "$k.wav"
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoice($voice)
    $synth.Rate = -1
    $synth.SetOutputToWaveFile($wav)
    $synth.Speak($txt)
    $synth.Dispose()
    Write-Output ("{0}  <- {1}  ({2:N0} KB)" -f $wav, $voice, ((Get-Item $wav).Length / 1KB))
}
