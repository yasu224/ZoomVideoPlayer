"""画面の書き換え (垂直同期) に合わせてコマを選ぶための道具。Qt を使わない (テストしやすいように)。

GPU 描画では、表と裏の画面を入れ替えた知らせ (frameSwapped) を受けた直後に次のコマを選ぶ。
ただしこの知らせは実際の書き換えより 0〜9 ミリ秒ほど遅れて届き、その遅れが毎回違う
(144Hz の 1 回分 6.9 ミリ秒を超えることもある)。そのため
- 時刻は、知らせの時刻から揺れを除いて推定する (VsyncTimer)。
- 動画と画面の速さが同じ (か整数倍) なら、壁時計ではなく書き換え 1 回につきちょうど決まったコマ数
  進める (VsyncLock)。知らせの遅れから「書き換えを飛ばしたか」を判断すると、遅れただけの知らせを
  飛ばしたと取り違えてコマが飛ぶので、それはしない。本当に遅れたときだけ、壁時計との差で追いつく。
"""
import math


def vsync_lock_step(fps, speed, hz, tolerance=0.01):
    """動画の進み (fps × 速度) が画面の Hz とほぼ同じか整数倍・整数分の 1 なら、書き換え 1 回で進める
    動画内の秒を返す (そうでなければ None)。

    壁時計でコマを選ぶと、画面の実際の Hz がわずかにずれている分 (144Hz と表示される画面が実際は
    143.97Hz など) だけ、コマの切り替わりと書き換えの位置関係が少しずつ動く。速さが同じだと、
    重なった所でコマ飛びと二度出しが続く。この場合は書き換え 1 回につきちょうど 1 コマ
    (や 2 コマ、半コマ) 進めて画面に合わせる。
    """
    if fps <= 0 or speed <= 0 or hz <= 0:
        return None
    ratio = fps * speed / hz              # 書き換え 1 回あたりのコマ数
    for k in (1, 2, 3, 4):
        for frames in (k, 1 / k):
            if abs(ratio / frames - 1) < tolerance:
                return frames / fps
    return None


class VsyncTimer:
    """書き換えの知らせごとに update() を呼ぶと、直前の書き換えの推定時刻 (perf_counter の秒) を返す。

    推定は「前回 + 1 回分の間隔」とし、知らせが予想より早く来たら推定を早める (遅れの少ない側に
    合わせる)、遅く来たら少しだけ遅らせる (Hz のわずかなずれを追う)。知らせの間隔が大きく空いたら
    書き換えを飛ばしたとみなして進める。長く途切れたら届いた時刻からやり直す。
    """

    def __init__(self):
        self.est = None

    def reset(self):
        self.est = None

    def update(self, measured, period):
        if self.est is None or period <= 0:
            self.est = measured
            return measured
        pred = self.est + period
        err = measured - pred
        if err > 4 * period or err < -period:     # 止まっていた・時計が飛んだ
            self.est = measured
            return measured
        skipped = math.floor((err + 0.3 * period) / period)
        if skipped > 0:                           # 書き換えを飛ばした (描画が間に合わなかった)
            pred += skipped * period
            err -= skipped * period
        pred += 0.5 * err if err < 0 else 0.02 * err
        self.est = pred
        return pred


class VsyncLock:
    """書き換え 1 回につき step 秒ずつ動画時刻を進める (vsync_lock_step で合わせるとき)。

    本当に描画が遅れて書き換えを飛ばすと、そのぶん壁時計 (推定した書き換え時刻 × 速度) より遅れる。
    遅れが 1.5 回分を超えたら、まとめて追いつく (そこでコマが飛ぶ)。画面の実際の Hz がわずかに
    違う分のずれも、たまったらここで 1 コマ分追いつく。
    """

    def __init__(self, step, speed):
        self.step, self.speed = step, speed
        self.t = None             # 直前の書き換えでの動画時刻
        self.ref = None           # 壁時計と比べる基準 (書き換え時刻, 動画時刻)

    def rebase(self, wall, t):
        """時計を合わせ直したとき (最初のコマ、ループの折り返し、速度の変更)。"""
        self.t = t
        self.ref = (wall, t)

    def advance(self, wall):
        """書き換え 1 回分進め、その書き換えでの動画時刻を返す。"""
        self.t += self.step
        ideal = self.ref[1] + (wall - self.ref[0]) * self.speed
        lag = ideal - self.t
        if lag > 1.5 * self.step:
            self.t += self.step * math.floor(lag / self.step - 0.5)
        elif lag < -4 * self.step:                # 推定時刻がやり直されたなど
            self.ref = (wall, self.t)
        return self.t
