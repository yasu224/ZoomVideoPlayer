"""メインウィンドウの映像入力 (カメラ・Blackmagic の SDI 入力) の部分: 入力の選択、表示、静止、録画、統計。

MainWindow がこのクラスを受け継ぐ。再生の流れ (on_swapped / on_timer / update_stats / toggle_play) から
入力を表示している間だけ呼ばれる。入力そのもの (機器とのやり取り・録画の書き出し) は player_live。
"""
import os
import sys
import time

from PySide6.QtCore import QCameraPermission, QSettings, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

import player_capture as capture
import player_live
from player_core import YUV_FORMATS, yuv_samples
from player_view import GpuWindow, white_balance

WB_INTERVAL = 0.2     # 自動WB: 色の平均を測り直す間隔 (秒)
WB_SMOOTH = 0.3       # 1 回にどれだけ新しい倍率へ寄せるか (急に色が変わってちらつかないように)


def _fmt():
    # 時刻と fps の書き方は player_gui のものを使う (player_gui がこのモジュールを読み込むので、使うときに読む)
    from player_gui import fmt_fps, fmt_time
    return fmt_fps, fmt_time


class LiveMixin:
    def refresh_inputs(self):
        """入力のメニューを、今つながっている入力で作り直す (選んでいる入力は残す)。"""
        combo = self.input_combo
        current = combo.currentData()
        if isinstance(self.view, GpuWindow):
            devices, note = player_live.list_inputs()
        else:
            devices, note = [], "入力の表示は GPU 描画のときだけ使えます"
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("動画ファイル", None)
        keep = 0
        for d in devices:
            if d.same(current):
                d = current                   # 読んだ形式の一覧と選んだ形式を持っているので、同じものを使い続ける
                keep = combo.count()
            combo.addItem(d.label(), d)
        if note:
            combo.addItem(note, None)
            combo.model().item(combo.count() - 1).setEnabled(False)
        if current is not None and keep == 0:     # 選んでいた入力が外れた
            combo.insertItem(1, current.label(), current)
            keep = 1
        combo.setCurrentIndex(keep)
        combo.blockSignals(False)

    def input_changed(self):
        device = self.input_combo.currentData()
        if device is not None and self.live is not None and device.same(self.live.device):
            return
        self.exit_live()
        if device is None:
            return self.show_file()
        if device.kind == "camera":
            return self.with_camera_permission(device)
        self.open_live(device)

    # カメラを使う前に OS の許可が要る (macOS。Windows はプライバシーの設定で切られていると使えない)。
    # macOS で許可されていないと、FFmpeg はダイアログも出さずにコマを待ち続け、「信号なし」のままになる。
    # カメラを開く前に確かめ、まだ聞いていなければ許可のダイアログを出す (許可の要らない OS では常に許可)
    def with_camera_permission(self, device):
        app = QApplication.instance()
        permission = QCameraPermission()
        status = app.checkPermission(permission)
        if status == Qt.PermissionStatus.Granted:
            return self.open_live(device)
        if status == Qt.PermissionStatus.Undetermined:
            def answered(result):
                if self.input_combo.currentData() is device:     # 待っている間に別の入力を選んでいなければ
                    self.with_camera_permission(device)
            app.requestPermission(permission, self, answered)
            return
        self.show_input_file()
        where = ("設定 → プライバシーとセキュリティ → カメラ で、デスクトップ アプリがカメラを使えるようにしてください。"
                 if sys.platform == "win32" else
                 "システム設定 → プライバシーとセキュリティ → カメラ で、このアプリを許可してください。")
        QMessageBox.warning(self, "入力", f"カメラの使用が許可されていません。\n{where}")

    def open_live(self, device):
        try:
            if device.kind == "camera" and device.modes is None:
                self.restore_camera_mode(device)
            source = device.open()
        except Exception as e:
            self.show_input_file()
            QMessageBox.warning(self, "入力", f"{device.label()} を開けません。\n{e}")
            return
        self.enter_live(source)

    # ---------- カメラの形式 (解像度・fps) ----------
    @staticmethod
    def camera_mode_setting(device):
        return "live/camera_mode/" + device.name.replace("/", "_").replace("\\", "_")

    def restore_camera_mode(self, device):
        """カメラの使える形式を読み、前に選んだ形式があればそれを使う (無ければ自動)。"""
        modes = device.load_modes()
        saved = self.settings().value(self.camera_mode_setting(device), "", type=str)
        device.mode = next((m for m in modes if capture.mode_key(m) == saved), None)

    def fill_camera_modes(self, device):
        """形式のメニューを、そのカメラの形式で作り直す。"""
        combo = self.mode_combo
        combo.blockSignals(True)
        combo.clear()
        modes = device.modes or []
        best = capture.best_mode(modes)
        combo.addItem(f"自動 ({capture.mode_label(best)})" if best else "自動", None)
        for i, m in enumerate(modes):         # 番号で持つ (Qt に渡すとタプルがリストに変わるため)
            combo.addItem(capture.mode_label(m), i)
            if m == device.mode:
                combo.setCurrentIndex(combo.count() - 1)
        combo.setEnabled(bool(modes))
        combo.blockSignals(False)

    def camera_mode_changed(self):
        """形式を選び直した: 覚えておき、その形式でカメラを開き直す。"""
        source = self.live
        if source is None or source.device.kind != "camera":
            return
        device = source.device
        index = self.mode_combo.currentData()
        device.mode = None if index is None else device.modes[index]
        settings = self.settings()
        if device.mode is None:
            settings.remove(self.camera_mode_setting(device))
        else:
            settings.setValue(self.camera_mode_setting(device), capture.mode_key(device.mode))
        self.exit_live()
        self.open_live(device)

    def enter_live(self, source):
        """入力の表示を始める。動画ファイルの再生は止め、書き換えのたびに新しいコマを出す。"""
        self.pause()
        self.arm_timer.stop()
        self.live = source
        self.live_frozen = False
        self.live_received = source.stats()[0]
        self.play_btn.setText("静止")
        self.play_btn.setToolTip("入力の表示を止める / 再開する (Space)")
        self.name_label.setText(f"入力: {source.name}")
        self.view.picture = None
        self.view.placeholder = f"{source.name}\n映像を待っています…"
        self.set_wb(None)
        # カメラ (macOS が渡す NV12) も SDI も、BT.709 の限定範囲 (16〜235)。前に開いた動画の設定を残さない
        self.set_view_color(True, False)
        if source.device.kind == "camera":
            self.fill_camera_modes(source.device)
        self.set_enabled(bool(self.src))
        self.update_sdi_sequence()
        self.timer.start()
        self.view.render_soon()

    def exit_live(self):
        """入力の表示をやめる (録画も止める)。"""
        if self.live is None:
            return
        self.stop_recording()
        self.timer.stop()
        self.live.close()
        self.live = None
        self.live_frozen = False
        self.rec_btn.setChecked(False)
        self.play_btn.setText("▶ 再生")
        self.play_btn.setToolTip("再生 / 一時停止 (Space)")
        self.view.placeholder = "動画ファイルをここにドロップ\nまたは「動画を開く」から選択"
        self.set_wb(None)
        self.set_enabled(bool(self.src))
        self.update_sdi_sequence()

    def show_input_file(self):
        """入力のメニューを「動画ファイル」に戻す (入力の表示もやめる)。"""
        self.input_combo.blockSignals(True)
        self.input_combo.setCurrentIndex(0)
        self.input_combo.blockSignals(False)
        self.exit_live()
        self.show_file()

    def show_file(self):
        """開いていた動画の、止めていた位置のコマを出し直す。"""
        if self.src and self.cur_frame is not None:
            fmt_fps, _ = _fmt()
            self.name_label.setText(f"{self.path}   {self.src.width}×{self.src.height}  {fmt_fps(self.src.fps)} fps")
            self.set_view_color(self.src.bt709, self.src.full_range)
            self.view.set_picture(self.cur_frame)
        else:
            self.name_label.setText("")
            self.view.picture = None
        self.view.update()

    def live_tick(self):
        """書き換えのたびに、入力の新しいコマがあれば出す (静止中は出さない)。"""
        shown = self.view.isExposed()
        if not self.live_frozen:
            picture = self.live.latest()
            if picture is not None:
                self.view.set_picture(picture)
                if self.wb_check.isChecked():
                    self.update_wb(picture)
                if self.recorder:
                    self.recorder.push(picture)
            if shown:
                self.view.render_soon()
            else:
                self.hidden_paint.start(max(1, round(1000 / self.hz)))

    def set_live_frozen(self, frozen):
        """入力の表示を止める / 再開する。止めている間も、拡大やピクセル表示はそのまま使える。"""
        if not self.live or frozen == self.live_frozen:
            return
        self.live_frozen = frozen
        if frozen and self.view.picture is not None:
            # 表示中のバッファは入力が使い回すので、止めるコマは写しを持つ
            self.view.set_picture(self.view.picture.copy())
        self.play_btn.setText("ライブ" if frozen else "静止")
        self.update_sdi_sequence()
        self.view.render_soon()

    def record_latest(self):
        """静止中に、入力の新しいコマを録画だけに渡す。"""
        if self.recorder and self.live:
            picture = self.live.latest()
            if picture is not None:
                self.recorder.push(picture)

    def toggle_recording(self):
        if not self.rec_btn.isChecked():
            return self.stop_recording()
        source = self.live
        w, h = source.size if source else (0, 0)
        if not source or not source.signal or not w:
            self.rec_btn.setChecked(False)
            QMessageBox.warning(self, "録画", "入力に映像が届いていないため、録画を始められません。")
            return
        default = os.path.join(os.path.expanduser("~/Movies"), time.strftime("ZoomVideoPlayer_%Y%m%d_%H%M%S.mov"))
        path, _ = QFileDialog.getSaveFileName(self, "録画の保存先", default, "QuickTime (*.mov)")
        if not path:
            self.rec_btn.setChecked(False)
            return
        try:
            self.recorder = player_live.Recorder(path, source.fmt, w, h, source.frame_rate())
        except Exception as e:
            self.rec_btn.setChecked(False)
            QMessageBox.warning(self, "録画", f"録画を始められません。\n{e}")
            return
        self.rec_btn.setText("■ 録画停止")

    def stop_recording(self):
        recorder, self.recorder = self.recorder, None
        self.rec_btn.setText("● 録画")
        self.rec_btn.setChecked(False)
        if recorder is None:
            return
        recorder.stop()
        note = f"{recorder.written} コマを書き出しました"
        if recorder.dropped:
            note += f" (書き出しが追いつかず {recorder.dropped} コマを落としました)"
        if recorder.error:
            QMessageBox.warning(self, "録画", f"録画中に問題がありました。\n{recorder.error}\n{note}")
        else:
            QMessageBox.information(self, "録画", f"{recorder.path}\n{note}")

    # ---------- 自動WB (ホワイトバランス) ----------
    @staticmethod
    def settings():
        return QSettings("ZoomVideoPlayer", "ZoomVideoPlayer")

    def wb_toggled(self, on):
        self.settings().setValue("live/auto_wb", on)
        self.set_wb(None)
        self.view.update()

    def set_view_color(self, bt709, full_range):
        if isinstance(self.view, GpuWindow):          # CPU 描画は色の変換を PyAV に任せている
            self.view.set_color(bt709, full_range)

    def set_wb(self, wb):
        self.wb_at = 0.0
        if isinstance(self.view, GpuWindow):
            self.view.wb = wb

    def wb_label(self):
        wb = getattr(self.view, "wb", None)
        if not self.wb_check.isChecked() or not wb:
            return ""
        (r, _, b), (rk, _, bk) = wb
        return f" ・ 自動WB R×{r:.2f}{rk:+.2f} B×{b:.2f}{bk:+.2f}"

    def update_wb(self, picture):
        """コマから補正 (色ごとの倍率とずれ) を求め、少しずつ寄せる (表示と SDI 出力に効く。録画には効かない)。"""
        now = time.monotonic()
        if not isinstance(self.view, GpuWindow) or picture.fmt not in YUV_FORMATS or now - self.wb_at < WB_INTERVAL:
            return
        self.wb_at = now
        samples = yuv_samples(picture.fmt, picture.w, picture.h, picture.data)
        target = white_balance(samples, self.view.bt709, self.view.full_range, YUV_FORMATS[picture.fmt][3])
        if target is None:              # 明るさの幅が狭すぎて求められない (信号なしの黒など): 今のまま
            return
        old = self.view.wb or target
        self.view.wb = tuple(tuple(o + WB_SMOOTH * (t - o) for o, t in zip(olds, news))
                             for olds, news in zip(old, target))

    def live_stats(self, shown_fps):
        """入力を表示している間の、右上の統計。"""
        source = self.live
        received, _ = source.stats()
        got, self.live_received = received - self.live_received, received
        hz = self.hz = self.refresh_rate()
        state = "静止中" if self.live_frozen else f"表示 {shown_fps:.1f} fps"
        text = (f"入力 {source.status()} ・ 受け取り {got / max(self.stats_dt, 1e-3):.1f} fps ・ {state}\n"
                f"画面 {round(hz)} Hz ・ {self.view.renderer}{self.sdi_label()}{self.wb_label()}")
        warn = not source.signal or bool(source.error)
        effects = getattr(source, "effects", None)
        if effects:
            text += f"\nmacOS のビデオエフェクト: {'・'.join(effects)} (コントロールセンターで切れます)"
            warn = True
        if self.recorder:
            r = self.recorder
            _, fmt_time = _fmt()
            text += f"\n● 録画中 {fmt_time(r.elapsed())[:-4]} ・ {r.codec} ・ 書き出し {r.written} コマ"
            if r.dropped:
                text += f" ・ 落としたコマ {r.dropped}"
                warn = True
        self.view.stats_text = text
        self.view.stats_warn = warn
        self.update_sdi_sequence()            # 入力の形式 (fps) が変わったときのため
        self.view.update()
