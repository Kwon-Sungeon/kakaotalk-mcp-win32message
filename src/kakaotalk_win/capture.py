"""Window capture (PrintWindow) to PNG and optional Windows built-in OCR.

PrintWindow asks the window to render itself into our bitmap, so it works for
windows hidden behind others. Minimized windows have nothing to render; the
caller decides whether to restore them (without activation) first.
"""

import contextlib
import ctypes
import json
import os
import queue
import struct
import subprocess
import tempfile
import threading
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
_temporary = set()  # paths save_png created without output_path in this process


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
    path = str(Path(target.name))
    if not output_path:
        _temporary.add(path)
    return path


def is_temporary(path):
    """True only for a temporary capture save_png made in this process (never for a caller's output_path)."""
    return bool(path) and str(path) in _temporary


def discard(paths):
    """Delete the temporary captures among `paths`; returns how many were removed."""
    removed = 0
    for path in paths or ():
        if is_temporary(path):
            _temporary.discard(str(path))
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


_OCR_COMMON = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
function Recognize($image, $language, [int]$zoom) {
  $lang = New-Object Windows.Globalization.Language($language)
  if (-not [Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) { return @{ status = 'ocr_language_unavailable' } }
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($image)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  try {
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
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
  } finally { $stream.Dispose() }
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
  @{ status = 'ok'; language = $language; lines = @($lines) }
}
"""

_OCR_SCRIPT = _OCR_COMMON + r"""
Recognize $env:KAKAOTALK_OCR_IMAGE $env:KAKAOTALK_OCR_LANG $env:KAKAOTALK_OCR_ZOOM | ConvertTo-Json -Depth 6 -Compress
"""

# One JSON request per stdin line, one compact JSON reply per stdout line; a failing image does not end the loop.
_OCR_LOOP_SCRIPT = _OCR_COMMON + r"""
while ($null -ne ($request = [Console]::In.ReadLine())) {
  try {
    $request = ConvertFrom-Json $request
    $reply = Recognize $request.image $request.lang $request.zoom | ConvertTo-Json -Depth 6 -Compress
  } catch { $reply = @{ status = 'ocr_failed'; detail = "$_" } | ConvertTo-Json -Compress }
  [Console]::Out.WriteLine($reply)
  [Console]::Out.Flush()
}
"""

OCR_TIMEOUT = 30  # seconds per image
_POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_local = threading.local()


class _OcrProcess:
    """A PowerShell process that keeps the OCR runtime loaded and answers one image per line."""

    def __init__(self):
        self.process = subprocess.Popen(_POWERSHELL + [_OCR_LOOP_SCRIPT], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        self.replies = queue.Queue()
        threading.Thread(target=self._read, name="kakaotalk-win-ocr", daemon=True).start()

    def _read(self):
        with self.process.stdout as stream:
            for line in stream:
                self.replies.put(line)
        self.replies.put(None)  # the process ended

    def ask(self, image, language, zoom):
        """The reply as a dict, or None when the process timed out, died or answered garbage."""
        request = json.dumps({"image": image, "lang": language, "zoom": int(zoom)}, ensure_ascii=True)
        try:
            self.process.stdin.write(request.encode("ascii") + b"\n")
            self.process.stdin.flush()
            line = self.replies.get(timeout=OCR_TIMEOUT)
            value = json.loads(line.decode("utf-8").lstrip("﻿")) if line else None
        except (OSError, ValueError, queue.Empty):
            value = None
        return value if isinstance(value, dict) else None

    def close(self):
        self.process.kill()
        self.process.wait()
        with contextlib.suppress(OSError):
            self.process.stdin.close()


class _OcrSession:
    def __init__(self):
        self.worker = None

    def ask(self, image, language, zoom):
        try:
            if self.worker is None:
                self.worker = _OcrProcess()
            value = self.worker.ask(image, language, zoom)
        except OSError:
            value = None
        if value is None and self.worker is not None:
            self.close()  # a later ocr() in this session starts a fresh process
        return value

    def close(self):
        if self.worker is not None:
            worker, self.worker = self.worker, None
            worker.close()


@contextlib.contextmanager
def ocr_session():
    """Within this block (re-entrant, per thread) ocr() reuses one PowerShell process instead of
    starting one per image, which saves most of the ~1.5 s each call costs. Ended when the outermost
    block exits."""
    outermost = getattr(_local, "session", None) is None
    if outermost:
        _local.session = _OcrSession()
    try:
        yield
    finally:
        if outermost:
            session, _local.session = _local.session, None
            session.close()


def ocr(png_path, language="ko", zoom=1):
    """Run the Windows built-in OCR engine (Windows.Media.Ocr) on a local PNG.

    Uses Windows PowerShell 5.1 to reach the WinRT API; no network, no extra install.
    Returns {"status": "ok", "lines": [{text, left, top, right, bottom, words: [{text, left, ...}]}, ...]}.
    zoom > 1 enlarges the image (cubic) inside the decoder before recognition; the returned
    coordinates are divided back, so they always refer to the PNG as saved.
    """
    image = str(Path(png_path).resolve())
    session = getattr(_local, "session", None)
    value = session.ask(image, language, zoom) if session else None
    if value is None:
        value = _ocr_once(image, language, zoom)
    if isinstance(value.get("lines"), dict):
        value["lines"] = [value["lines"]]
    if zoom > 1:
        value["lines"] = [_unzoom(line, zoom) for line in value.get("lines") or []]
    return value


def _ocr_once(image, language, zoom):
    env = {**os.environ, "KAKAOTALK_OCR_IMAGE": image, "KAKAOTALK_OCR_LANG": language,
           "KAKAOTALK_OCR_ZOOM": str(int(zoom))}
    try:
        result = subprocess.run(_POWERSHELL + [_OCR_SCRIPT], capture_output=True, timeout=OCR_TIMEOUT, env=env,
                                creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "ocr_failed", "errorType": type(error).__name__}
    output = result.stdout.decode("utf-8", errors="replace").lstrip("﻿").strip()
    try:
        value = json.loads(output.splitlines()[-1]) if output else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return {"status": "ocr_failed", "detail": result.stderr.decode("utf-8", errors="replace")[-300:]}
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
