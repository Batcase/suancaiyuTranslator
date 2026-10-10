# 用系统 TTS 播放一段英文语音，用于验证"回环采集 → 识别 → 翻译"整条链路。
# 用法： pwsh -File tools\speak_test.ps1 [-Text "hello world"] [-Repeat 1]
param(
    [string]$Text = "Hello everyone, welcome back to the stream. Today we are going to talk about the new graphics card, and I will show you how it performs in four K gaming. Let me know in the chat where you are watching from.",
    [int]$Repeat = 1,
    [int]$Rate = 0
)

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = $Rate
$synth.Volume = 100

Write-Host "可用语音:"
$synth.GetInstalledVoices() | ForEach-Object { Write-Host ("  - " + $_.VoiceInfo.Name + " [" + $_.VoiceInfo.Culture + "]") }
# 优先选英文语音
$en = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'en*' } | Select-Object -First 1
if ($en) { $synth.SelectVoice($en.VoiceInfo.Name) }
Write-Host "使用语音: $($synth.Voice.Name)"

for ($i = 1; $i -le $Repeat; $i++) {
    Write-Host "[speak $i/$Repeat] $Text"
    $synth.Speak($Text)
    if ($i -lt $Repeat) { Start-Sleep -Milliseconds 400 }
}
$synth.Dispose()
Write-Host "TTS 播放结束"
