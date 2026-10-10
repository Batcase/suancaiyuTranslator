# 用系统 TTS 合成一段英文语音并保存为 16kHz 单声道 wav，用于离线识别测试。
# 用法： pwsh -File tools\make_sample.ps1 [-Out samples\en_test.wav]
param(
    [string]$Out = "samples\en_test.wav",
    [string]$Text = "Hey chat, welcome back to the stream. Today we are testing the new graphics card, and I will show you how it performs in four K gaming. Let me know in the chat where you are watching from."
)

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$en = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'en*' } | Select-Object -First 1
if ($en) { $synth.SelectVoice($en.VoiceInfo.Name) }
$synth.Rate = 0

$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$full = [System.IO.Path]::GetFullPath($Out)

# SetOutputToWaveFile 写出的通常是 22.05kHz 16bit 单声道，faster-whisper 会自行重采样
$synth.SetOutputToWaveFile($full)
$synth.Speak($Text)
$synth.SetOutputToNull()
$synth.Dispose()

$f = Get-Item $full
Write-Host "saved: $full ($([math]::Round($f.Length/1024,1)) KB)"
