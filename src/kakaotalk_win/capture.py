"""Window capture (PrintWindow) to PNG and optional Windows built-in OCR.

PrintWindow asks the window to render itself into our bitmap, so it works for
windows hidden behind others. Minimized windows have nothing to render; the
caller decides whether to restore them (without activation) first.
"""

import ctypes
import json
import os
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path

from . import win32 as w

BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def grab(hwnd):
    """Return (width, height, BGRA bytes) of the whole window, at the size the app itself draws."""
    with w.window_dpi(hwnd):
        return _grab(hwnd)


def _grab(hwnd):
    left, top, right, bottom = w.rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("Window has no visible area to capture")
    screen = w.user32.GetDC(None)
    memory = w.gdi32.CreateCompatibleDC(screen)
    bitmap = w.gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = w.gdi32.SelectObject(memory, bitmap)
    try:
        if not w.user32.PrintWindow(hwnd, memory, w.PW_RENDERFULLCONTENT):
            raise OSError(ctypes.get_last_error(), "PrintWindow failed")
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, BI_RGB, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not w.gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(header), DIB_RGB_COLORS):
            raise OSError(ctypes.get_last_error(), "GetDIBits failed")
        return width, height, buffer.raw
    finally:
        w.gdi32.SelectObject(memory, previous)
        w.gdi32.DeleteObject(bitmap)
        w.gdi32.DeleteDC(memory)
        w.user32.ReleaseDC(None, screen)


def crop(image, box):
    width, height, data = image
    left, top, right, bottom = (max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3]))
    rows = [data[(y * width + left) * 4:(y * width + right) * 4] for y in range(top, bottom)]
    return right - left, bottom - top, b"".join(rows)


def png_bytes(image):
    width, height, bgra = image
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        row = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        raw.append(0)
        raw.extend(rgb)

    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


TEMP_PREFIX = "kakaotalk-win-"
TEMP_MAX_AGE = 3600  # seconds; captures show other people's conversations, so they never pile up


def save_png(image, output_path=None):
    """Write the PNG to output_path, which must not exist yet, or to a new private temporary file.

    Temporary captures are meant to be deleted by the caller once used (see discard). As a
    backstop, temporary captures older than an hour are removed whenever a new one is made.
    """
    data = png_bytes(image)
    if output_path:
        target = open(output_path, "xb")  # "x" refuses to overwrite an existing file
    else:
        sweep_temporary()
        target = tempfile.NamedTemporaryFile(prefix=TEMP_PREFIX, suffix=".png", delete=False)
    with target:
        target.write(data)
    return str(Path(target.name))


def is_temporary(path):
    """True for a capture this package wrote to the temp folder (never for a caller's output_path)."""
    try:
        candidate = Path(path)
        return (candidate.name.startswith(TEMP_PREFIX) and candidate.suffix == ".png"
                and candidate.parent.resolve() == Path(tempfile.gettempdir()).resolve())
    except (OSError, TypeError, ValueError):
        return False


def discard(paths):
    """Delete the temporary captures among `paths`; returns how many were removed."""
    removed = 0
    for path in paths or ():
        if path and is_temporary(path):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def sweep_temporary(max_age=TEMP_MAX_AGE):
    """Remove temporary captures older than max_age seconds (left by the CLI or an interrupted call)."""
    cutoff = time.time() - max_age
    try:
        entries = list(Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*.png"))
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def difference(a, b, step=4):
    """Fraction of sampled pixels that differ between two same-size captures."""
    if a[:2] != b[:2]:
        return 1.0
    da, db = a[2], b[2]
    total = changed = 0
    for offset in range(0, len(da), 4 * step):
        total += 1
        if da[offset:offset + 3] != db[offset:offset + 3]:
            changed += 1
    return changed / max(1, total)


_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
$lang = New-Object Windows.Globalization.Language($env:KAKAOTALK_OCR_LANG)
if (-not [Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) { '{"status":"ocr_language_unavailable"}'; exit 0 }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:KAKAOTALK_OCR_IMAGE)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$zoom = [int]$env:KAKAOTALK_OCR_ZOOM
if ($zoom -gt 1) {
  # Smooth (cubic) enlargement: small glyphs drawn at 100% scale are read far better at 2x.
  $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
  $transform.ScaledWidth = $decoder.PixelWidth * $zoom
  $transform.ScaledHeight = $decoder.PixelHeight * $zoom
  $transform.InterpolationMode = [Windows.Graphics.Imaging.BitmapInterpolationMode]::Cubic
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
    [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied, $transform,
    [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
    [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
} else {
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
}
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = foreach ($line in $result.Lines) {
  $xs = $line.Words | ForEach-Object { $_.BoundingRect }
  $words = foreach ($word in $line.Words) { $r = $word.BoundingRect
    [pscustomobject]@{ text = $word.Text; left = [int]$r.X; top = [int]$r.Y; right = [int]($r.X + $r.Width);
      bottom = [int]($r.Y + $r.Height) } }
  [pscustomobject]@{ text = $line.Text; left = [int]($xs | Measure-Object X -Minimum).Minimum;
    top = [int]($xs | Measure-Object Y -Minimum).Minimum;
    right = [int](($xs | ForEach-Object { $_.X + $_.Width }) | Measure-Object -Maximum).Maximum;
    bottom = [int](($xs | ForEach-Object { $_.Y + $_.Height }) | Measure-Object -Maximum).Maximum;
    words = @($words) }
}
$stream.Dispose()
[Console]::OutputEncoding = [Text.Encoding]::UTF8
@{ status = 'ok'; language = $env:KAKAOTALK_OCR_LANG; lines = @($lines) } | ConvertTo-Json -Depth 6 -Compress
"""


def ocr(png_path, language="ko", zoom=1):
    """Run the Windows built-in OCR engine (Windows.Media.Ocr) on a local PNG.

    Uses Windows PowerShell 5.1 to reach the WinRT API; no network, no extra install.
    Returns {"status": "ok", "lines": [{text, left, top, right, bottom, words: [{text, left, ...}]}, ...]}.
    zoom > 1 enlarges the image (cubic) inside the decoder before recognition; the returned
    coordinates are divided back, so they always refer to the PNG as saved.
    """
    env = {**os.environ, "KAKAOTALK_OCR_IMAGE": str(Path(png_path).resolve()), "KAKAOTALK_OCR_LANG": language,
           "KAKAOTALK_OCR_ZOOM": str(int(zoom))}
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-Command", _OCR_SCRIPT], capture_output=True, timeout=30, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "ocr_failed", "errorType": type(error).__name__}
    output = result.stdout.decode("utf-8", errors="replace").strip()
    try:
        value = json.loads(output.splitlines()[-1]) if output else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return {"status": "ocr_failed", "detail": result.stderr.decode("utf-8", errors="replace")[-300:]}
    if isinstance(value.get("lines"), dict):
        value["lines"] = [value["lines"]]
    if zoom > 1:
        value["lines"] = [_unzoom(line, zoom) for line in value.get("lines") or []]
    return value


def _unzoom(item, zoom):
    scaled = dict(item, **{edge: int(item[edge] // zoom) for edge in ("left", "top", "right", "bottom")
                           if isinstance(item.get(edge), (int, float))})
    words = item.get("words")
    if isinstance(words, dict):
        words = [words]
    if words:
        scaled["words"] = [_unzoom(word, zoom) for word in words]
    return scaled


# Grey on white: everything at or below 150 becomes black, the rest is stretched up to white.
_DARKEN = bytes(0 if value <= 150 else (value - 150) * 255 // 105 for value in range(256))


def darken(image):
    """Raise the contrast of grey text (time labels, previews) for OCR. Alpha 255 stays 255."""
    width, height, data = image
    return width, height, data.translate(_DARKEN)
