# 列出当前所有可见顶层窗口的标题与矩形，用于确认悬浮窗的真实位置和尺寸。
$src = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinEnum {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern int GetWindowLongW(IntPtr hWnd, int index);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
'@
Add-Type -TypeDefinition $src

$list = New-Object System.Collections.ArrayList
$cb = [WinEnum+EnumWindowsProc]{
  param($h, $l)
  if ([WinEnum]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 512
    [void][WinEnum]::GetWindowTextW($h, $sb, 512)
    $title = $sb.ToString()
    $r = New-Object WinEnum+RECT
    [void][WinEnum]::GetWindowRect($h, [ref]$r)
    $w = $r.Right - $r.Left; $hgt = $r.Bottom - $r.Top
    if ($w -gt 100 -and $hgt -gt 50) {
      $ex = [WinEnum]::GetWindowLongW($h, -20)
      [void]$list.Add([pscustomobject]@{
        Title = $title
        X = $r.Left; Y = $r.Top; W = $w; H = $hgt
        Topmost = ($ex -band 0x8) -ne 0
        ClickThrough = ($ex -band 0x20) -ne 0
        ToolWin = ($ex -band 0x80) -ne 0
      })
    }
  }
  return $true
}
[void][WinEnum]::EnumWindows($cb, [IntPtr]::Zero)
$list | Sort-Object Y | Format-Table -AutoSize | Out-String -Width 200
