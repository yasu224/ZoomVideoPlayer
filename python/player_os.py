"""OS ごとの違いがある処理: 画面のスレッドの優先度と、見えていない間も OS に遅らせないための設定。

macOS と Windows で別々の仕組みを ctypes で呼ぶ。ほかの OS や失敗したときは何もしない (再生は続ける)。
"""
import sys


def raise_ui_thread_priority():
    """画面側のスレッド (いま呼んだスレッド) の優先度を上げる。

    再生中はデコード用プロセスが CPU を大きく使う (240fps の 1080p ならほぼ全コア)。
    画面側は書き換えのたびに数ミリ秒だけ動けばよいが、その時刻に CPU をもらえないと
    書き換えを取りこぼす。プロセスごと優先度を変えるとデコードが他のアプリに負けて
    先読みが空になるので、画面のスレッドだけを上げる。
    """
    import ctypes
    if sys.platform == "darwin":
        # macOS: スレッドの QoS を「ユーザー操作中」(いちばん高い) にする
        try:
            libc = ctypes.CDLL(None)          # 起動中のプロセス (libSystem を含む) から探す
            QOS_CLASS_USER_INTERACTIVE = 0x21
            libc.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)
        except (OSError, AttributeError):
            pass
        return
    if sys.platform != "win32":
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
            if sys.platform == "darwin":
                self.token = self._mac_begin(reason)
            elif sys.platform == "win32":
                self._win_throttle(False)
                self.token = True
        except Exception as e:          # 動かなくても再生は続ける
            print(f"裏に回ったときの遅れを止められません: {e}")

    def stop(self):
        if self.token is None:
            return
        try:
            if sys.platform == "darwin":
                self._mac_end(self.token)
            elif sys.platform == "win32":
                self._win_throttle(True)
        except Exception as e:
            print(f"裏に回ったときの設定を戻せません: {e}")
        self.token = None

    # ---------- macOS (Objective-C のランタイムを ctypes で呼ぶ) ----------
    @staticmethod
    def _objc():
        import ctypes
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value

        def msg(restype, *argtypes):
            # arm64 では objc_msgSend を引数の型どおりの関数として呼ぶ必要がある
            return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(send)
        return ctypes, objc, msg

    def _mac_process_info(self):
        ctypes, objc, msg = self._objc()
        cls = objc.objc_getClass(b"NSProcessInfo")
        return ctypes, objc, msg, msg(ctypes.c_void_p)(cls, objc.sel_registerName(b"processInfo"))

    def _mac_begin(self, reason):
        ctypes, objc, msg, info = self._mac_process_info()
        text = msg(ctypes.c_void_p, ctypes.c_char_p)(
            objc.objc_getClass(b"NSString"), objc.sel_registerName(b"stringWithUTF8String:"), reason.encode())
        token = msg(ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)(
            info, objc.sel_registerName(b"beginActivityWithOptions:reason:"), self.MAC_OPTIONS, text)
        if not token:
            raise RuntimeError("beginActivityWithOptions に失敗しました")
        msg(ctypes.c_void_p)(token, objc.sel_registerName(b"retain"))      # stop まで持っておく
        return token

    def _mac_end(self, token):
        ctypes, objc, msg, info = self._mac_process_info()
        msg(None, ctypes.c_void_p)(info, objc.sel_registerName(b"endActivity:"), token)
        msg(None)(token, objc.sel_registerName(b"release"))

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
