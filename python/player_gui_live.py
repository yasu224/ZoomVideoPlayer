"""メインウィンドウの映像入力 (カメラ・Blackmagic の SDI 入力) の部分: 入力の選択、表示、静止、録画、統計。

MainWindow がこのクラスを受け継ぐ。再生の流れ (on_swapped / on_timer / update_stats / toggle_play) から
入力を表示している間だけ呼ばれる。入力そのもの (機器とのやり取り・録画の書き出し) は player_live。
"""
import os
import time

from PySide6.QtWidgets import QFileDialog, QMessageBox

import player_live
from player_view import GpuWindow


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
            combo.addItem(d.label(), d)
            if d.same(current):
                keep = combo.count() - 1
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
        try:
            source = device.open()
        except Exception as e:
            self.show_input_file()
            QMessageBox.warning(self, "入力", f"{device.label()} を開けません。\n{e}")
            return
        self.enter_live(source)

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

    def live_stats(self, shown_fps):
        """入力を表示している間の、右上の統計。"""
        source = self.live
        received, _ = source.stats()
        got, self.live_received = received - self.live_received, received
        hz = self.hz = self.refresh_rate()
        state = "静止中" if self.live_frozen else f"表示 {shown_fps:.1f} fps"
        text = (f"入力 {source.status()} ・ 受け取り {got / max(self.stats_dt, 1e-3):.1f} fps ・ {state}\n"
                f"画面 {round(hz)} Hz ・ {self.view.renderer}{self.sdi_label()}")
        warn = not source.signal or bool(source.error)
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
