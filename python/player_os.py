"""OS ごとの違いがある処理: どの OS かの判定、画面のスレッドの優先度、見えていない間も OS に
遅らせないための設定、macOS の Objective-C のランタイムの呼び出し。

macOS と Windows で別々の仕組みを ctypes で呼ぶ。ほかの OS や失敗したときは何もしない (再生は続ける)。
OS で分ける所は、sys.platform を直接見ずに IS_MAC / IS_WIN を使う。
このファイルは Qt も PyAV も読み込まない (デコードやカメラの別プロセスからも読むため)。
"""
import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


class ObjC:
    """macOS の Objective-C のランタイムを ctypes で呼ぶ。読めなければ OSError か AttributeError。

    frameworks: 先に読み込んでおくフレームワークの名前 (例: "AVFoundation")。
    """

    def __init__(self, *frameworks):
        import ctypes
        import ctypes.util
        self.ctypes = ctypes
        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
        for name in frameworks:
            ctypes.cdll.LoadLibrary(f"/System/Library/Frameworks/{name}.framework/{name}")
        lib.objc_getClass.restype = lib.sel_registerName.restype = ctypes.c_void_p
        lib.objc_getClass.argtypes = lib.sel_registerName.argtypes = [ctypes.c_char_p]
        lib.class_getClassMethod.restype = ctypes.c_void_p
        lib.class_getClassMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib = lib
        self._send = ctypes.cast(lib.objc_msgSend, ctypes.c_void_p).value

    def cls(self, name):
        return self.lib.objc_getClass(name.encode())

    def sel(self, name):
        return self.lib.sel_registerName(name.encode())

    def class_method(self, cls, name):
        """クラスメソッドがあればそのセレクタ、無ければ None (OS の版によって無いものがある)。"""
        sel = self.sel(name)
        return sel if self.lib.class_getClassMethod(cls, sel) else None

    def msg(self, restype, *argtypes):
        """objc_msgSend を、呼ぶメソッドの型どおりの関数にして返す。(受け手, セレクタ, 引数...) で呼ぶ。

        arm64 では可変長引数の関数として呼べないので、型を決めて呼ぶ必要がある。
        """
        c = self.ctypes
        return c.CFUNCTYPE(restype, c.c_void_p, c.c_void_p, *argtypes)(self._send)


def raise_ui_thread_priority():
    """画面側のスレッド (いま呼んだスレッド) の優先度を上げる。

    再生中はデコード用プロセスが CPU を大きく使う (240fps の 1080p ならほぼ全コア)。
    画面側は書き換えのたびに数ミリ秒だけ動けばよいが、その時刻に CPU をもらえないと
    書き換えを取りこぼす。プロセスごと優先度を変えるとデコードが他のアプリに負けて
    先読みが空になるので、画面のスレッドだけを上げる。
    """
    import ctypes
    if IS_MAC:
        # macOS: スレッドの QoS を「ユーザー操作中」(いちばん高い) にする
        try:
            libc = ctypes.CDLL(None)          # 起動中のプロセス (libSystem を含む) から探す
            QOS_CLASS_USER_INTERACTIVE = 0x21
            libc.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)
        except (OSError, AttributeError):
            pass
        return
    if not IS_WIN:
        return
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    THREAD_PRIORITY_HIGHEST = 2
    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)


class KeepRunning:
    """見えていない (最小化・ほかのウィンドウの後ろ) 間も、タイマーを遅らせずに動かしてもらう。

    SDI 出力中はモニターを見ていてアプリを裏に回すことがあるが、OS は見えていないアプリの
    タイマーをまとめて遅らせたり (macOS の App Nap とタイマーの合体)、遅くしたり (Windows 11 の
    効率モード) する。そうなると出力のコマが間に合わず、同じ絵が続く。
    macOS: NSProcessInfo の beginActivityWithOptions (ユーザーが始めた、遅れの許されない処理)。
    Windows: SetProcessInformation の電力の抑制 (実行速度とタイマーの精度) を外す。
    """

    # macOS: NSActivityUserInitiated | NSActivityLatencyCritical
    MAC_OPTIONS = 0x00FFFFFF | 0xFF00000000

    def __init__(self):
        self.token = None

    def start(self, reason):
        if self.token is not None:
            return
        try:
            if IS_MAC:
                self.token = self._mac_begin(reason)
            elif IS_WIN:
                self._win_throttle(False)
                self.token = True
        except Exception as e:          # 動かなくても再生は続ける
            print(f"裏に回ったときの遅れを止められません: {e}")

    def stop(self):
        if self.token is None:
            return
        try:
            if IS_MAC:
                self._mac_end(self.token)
            elif IS_WIN:
                self._win_throttle(True)
        except Exception as e:
            print(f"裏に回ったときの設定を戻せません: {e}")
        self.token = None

    # ---------- macOS (NSProcessInfo) ----------
    @staticmethod
    def _mac_process_info():
        objc = ObjC()
        return objc, objc.msg(objc.ctypes.c_void_p)(objc.cls("NSProcessInfo"), objc.sel("processInfo"))

    def _mac_begin(self, reason):
        objc, info = self._mac_process_info()
        c = objc.ctypes
        text = objc.msg(c.c_void_p, c.c_char_p)(objc.cls("NSString"), objc.sel("stringWithUTF8String:"),
                                                 reason.encode())
        token = objc.msg(c.c_void_p, c.c_uint64, c.c_void_p)(
            info, objc.sel("beginActivityWithOptions:reason:"), self.MAC_OPTIONS, text)
        if not token:
            raise RuntimeError("beginActivityWithOptions に失敗しました")
        objc.msg(c.c_void_p)(token, objc.sel("retain"))      # stop まで持っておく
        return token

    def _mac_end(self, token):
        objc, info = self._mac_process_info()
        objc.msg(None, objc.ctypes.c_void_p)(info, objc.sel("endActivity:"), token)
        objc.msg(None)(token, objc.sel("release"))

    # ---------- Windows ----------
    @staticmethod
    def _win_throttle(allow):
        import ctypes
        from ctypes import wintypes

        class PowerThrottling(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
        ProcessPowerThrottling = 4
        mask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
        # ControlMask の項目を OS に任せる (allow) か、StateMask=0 で抑制しないと決める
        state = PowerThrottling(1, 0 if allow else mask, 0)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        if not kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), ProcessPowerThrottling,
                                              ctypes.byref(state), ctypes.sizeof(state)):
            raise OSError(ctypes.get_last_error(), "SetProcessInformation")
