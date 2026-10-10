# 截取整个屏幕到 PNG，用于验证悬浮字幕窗的实际渲染效果。
# 用法： pwsh -File tools\screenshot.ps1 [-Out shot.png]
param(
    [string]$Out = "shot.png"
)

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$full = [System.IO.Path]::GetFullPath($Out)
$bmp.Save($full, [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()
Write-Host "saved: $full ($($bounds.Width)x$($bounds.Height))"
