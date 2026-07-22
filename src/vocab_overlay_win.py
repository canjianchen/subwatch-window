"""Always-on-top hard-word overlay for Windows using only Win32 APIs."""
import ctypes
from ctypes import wintypes
import signal

import config
import vocab_feed

LAYOUT_KEY = "vocab_overlay_rect"
MAX_SENTENCES = 4
MAX_WORDS = 16
MAX_AGE = 180.0
DEFAULT_W = 560
DEFAULT_H = 580
WINDOW_NAME = "SubWatchVocab"

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                            wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD), ("pt", POINT),
    ]


# ctypes otherwise assumes 32-bit integer arguments/returns. Explicit signatures
# are required for HWND, WPARAM and LPARAM on 64-bit Windows.
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.restype = wintypes.BOOL
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                            wintypes.HBRUSH]
user32.FillRect.restype = ctypes.c_int
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.InvalidateRect.argtypes = [wintypes.HWND,
                                  ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
user32.InvalidateRect.restype = wintypes.BOOL
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(wintypes.RECT), wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                               wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.UpdateWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateFontW.argtypes = ([ctypes.c_int] * 5 + [wintypes.DWORD] * 8 +
                              [wintypes.LPCWSTR])
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetTextColor.restype = wintypes.DWORD
gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR,
                                        ctypes.c_int, ctypes.POINTER(wintypes.SIZE)]
gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL


WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_NCHITTEST = 0x0084
HTCAPTION = 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT = 10, 11, 12, 13
HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 14, 15, 16, 17
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x00000002
SW_SHOW = 5
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
TRANSPARENT = 1
DT_LEFT = 0x0000
DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004


def _rgb(red, green, blue):
    return red | (green << 8) | (blue << 16)


def _signed_word(value):
    return ctypes.c_short(value & 0xFFFF).value


class VocabOverlay:
    def __init__(self):
        self.cfg = config.load_config()
        self.entries = []
        self._shown_key = None
        self.hwnd = None
        self.hinstance = kernel32.GetModuleHandleW(None)
        self.class_name = "SubWatchVocabWindow"
        self.bg_color = _rgb(16, 16, 20)
        self.bg_brush = gdi32.CreateSolidBrush(self.bg_color)
        self.term_font = gdi32.CreateFontW(
            -25, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        self.cn_font = gdi32.CreateFontW(
            -22, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
        self.hint_font = gdi32.CreateFontW(
            -18, 0, 0, 0, 400, 1, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        self._wndproc = WNDPROC(self._window_proc)

    def _saved_rect(self):
        saved = self.cfg.get(LAYOUT_KEY)
        if saved and len(saved) == 4:
            x, y, width, height = (int(value) for value in saved)
            return x, y, max(260, width), max(110, height)
        screen_w = user32.GetSystemMetrics(0)
        return max(20, screen_w - DEFAULT_W - 50), 80, DEFAULT_W, DEFAULT_H

    def _save(self):
        if not self.hwnd:
            return
        rect = wintypes.RECT()
        if user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
            self.cfg[LAYOUT_KEY] = [rect.left, rect.top,
                                    rect.right - rect.left, rect.bottom - rect.top]
            config.save_config(self.cfg)

    def _poll(self):
        entries = vocab_feed.recent(max_sentences=MAX_SENTENCES,
                                    max_words=MAX_WORDS, max_age=MAX_AGE)
        key = tuple((entry.get("term", ""), entry.get("cn", "")) for entry in entries)
        if key != self._shown_key:
            self.entries = entries
            self._shown_key = key
            self._fit_to_entries()
            user32.InvalidateRect(self.hwnd, None, True)

    def _fit_to_entries(self):
        """Keep a few words compact, then grow the window as more arrive."""
        if not self.hwnd:
            return
        rect = wintypes.RECT()
        if not user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
            return
        width = rect.right - rect.left
        rows = max(1, min(MAX_WORDS, len(self.entries)))
        target_height = min(DEFAULT_H, max(92, 30 + rows * 34))
        current_height = rect.bottom - rect.top
        if abs(current_height - target_height) < 2:
            return
        user32.SetWindowPos(
            self.hwnd, None, 0, 0, width, target_height,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)

    def _paint(self):
        paint = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(paint))
        rect = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        user32.FillRect(hdc, ctypes.byref(rect), self.bg_brush)
        gdi32.SetBkMode(hdc, TRANSPARENT)
        if not self.entries:
            gdi32.SelectObject(hdc, self.hint_font)
            gdi32.SetTextColor(hdc, _rgb(165, 165, 173))
            hint = "Hard words appear here as you watch"
            hint_rect = wintypes.RECT(18, 18, rect.right - 18, rect.bottom - 18)
            user32.DrawTextW(hdc, hint, -1, ctypes.byref(hint_rect),
                             DT_LEFT | DT_SINGLELINE | DT_VCENTER)
        else:
            y = 15
            for entry in self.entries:
                if y + 30 > rect.bottom:
                    break
                term = entry.get("term", "")
                cn = entry.get("cn", "")
                gdi32.SelectObject(hdc, self.term_font)
                gdi32.SetTextColor(hdc, _rgb(255, 255, 255))
                term_rect = wintypes.RECT(18, y, rect.right - 18, y + 32)
                user32.DrawTextW(hdc, term, -1, ctypes.byref(term_rect),
                                 DT_LEFT | DT_SINGLELINE | DT_VCENTER)
                size = wintypes.SIZE()
                gdi32.GetTextExtentPoint32W(hdc, term, len(term), ctypes.byref(size))
                if cn:
                    gdi32.SelectObject(hdc, self.cn_font)
                    gdi32.SetTextColor(hdc, _rgb(243, 201, 163))
                    cn_rect = wintypes.RECT(min(rect.right - 20, 30 + size.cx), y,
                                            rect.right - 18, y + 32)
                    user32.DrawTextW(hdc, cn, -1, ctypes.byref(cn_rect),
                                     DT_LEFT | DT_SINGLELINE | DT_VCENTER)
                y += 34
        user32.EndPaint(self.hwnd, ctypes.byref(paint))

    def _hit_test(self, lparam):
        point = POINT(_signed_word(lparam), _signed_word(lparam >> 16))
        user32.ScreenToClient(self.hwnd, ctypes.byref(point))
        rect = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        edge = 12
        left, right = point.x < edge, point.x >= rect.right - edge
        top, bottom = point.y < edge, point.y >= rect.bottom - edge
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
        return HTCAPTION

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_PAINT:
            self._paint()
            return 0
        if message == WM_TIMER:
            self._poll()
            return 0
        if message == WM_NCHITTEST:
            return self._hit_test(lparam)
        if message == WM_CLOSE:
            self._save()
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.KillTimer(hwnd, 1)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def run(self):
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = self.hinstance
        window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        window_class.hbrBackground = self.bg_brush
        window_class.lpszClassName = self.class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom and ctypes.get_last_error() != 1410:  # already registered
            raise ctypes.WinError(ctypes.get_last_error())
        x, y, width, height = self._saved_rect()
        ex_style = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(
            ex_style, self.class_name, WINDOW_NAME, WS_POPUP,
            x, y, width, height, None, None, self.hinstance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            alpha = float(self.cfg.get("vocab_overlay_bg", 0.22))
        except (TypeError, ValueError):
            alpha = 0.22
        opacity = int(190 + max(0.0, min(1.0, alpha)) * 60)
        user32.SetLayeredWindowAttributes(self.hwnd, 0, opacity, LWA_ALPHA)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.UpdateWindow(self.hwnd)
        user32.SetTimer(self.hwnd, 1, 300, None)
        self._poll()
        message = MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))


def main():
    overlay = VocabOverlay()
    signal.signal(signal.SIGINT, lambda *_args: overlay.close())
    signal.signal(signal.SIGTERM, lambda *_args: overlay.close())
    overlay.run()


if __name__ == "__main__":
    main()
