"""書き換えに合わせてコマを選ぶ道具 (player_vsync) のテスト。"""
import random
import unittest

from player_vsync import VsyncLock, VsyncTimer, vsync_lock_step


class LockStepTest(unittest.TestCase):
    def test_same_rate_locks_one_frame(self):
        self.assertAlmostEqual(vsync_lock_step(144, 1, 143.97), 1 / 144)
        self.assertAlmostEqual(vsync_lock_step(59.94, 1, 60), 1 / 59.94)

    def test_integer_ratios_lock(self):
        self.assertAlmostEqual(vsync_lock_step(72, 1, 144), 0.5 / 72)      # 1 コマを 2 回出す
        self.assertAlmostEqual(vsync_lock_step(240, 0.5, 60), 2 / 240)    # 書き換えごとに 2 コマ
        self.assertAlmostEqual(vsync_lock_step(120, 1, 60), 2 / 120)

    def test_other_ratios_do_not_lock(self):
        self.assertIsNone(vsync_lock_step(120, 1, 144))
        self.assertIsNone(vsync_lock_step(240, 1, 144))
        self.assertIsNone(vsync_lock_step(60, 1, 144))


class VsyncTimerTest(unittest.TestCase):
    def test_removes_notice_jitter(self):
        """知らせが 0〜5 ms ばらばらに遅れても、推定は書き換えの間隔どおりに進む。"""
        rnd = random.Random(1)
        period = 1 / 144
        timer = VsyncTimer()
        ests = [timer.update(k * period + rnd.uniform(0, 0.005), period) for k in range(300)]
        steps = [b - a for a, b in zip(ests[100:], ests[101:])]
        self.assertLess(max(abs(s - period) for s in steps), 0.0006)

    def test_skipped_refresh_moves_on(self):
        period = 1 / 144
        timer = VsyncTimer()
        for k in range(50):
            timer.update(k * period + 0.0005, period)
        est = timer.update(52 * period + 0.0005, period)   # 49 の次が 52 (2 回飛ばした)
        self.assertAlmostEqual(est, 52 * period, delta=0.001)

    def test_second_swap_in_same_refresh(self):
        """macOS の 60Hz の 4K モニターのように、1 回の書き換えに入れ替えが 2 回通る (4 ms と 12.6 ms の
        交互) とき、間隔が短いと分かった後は 2 回目を同じ書き換えとみなし、推定は書き換えの間隔どおりに進む。"""
        period = 1 / 60
        timer = VsyncTimer()
        ests = []
        for k in range(200):
            for measured, second in ((k * period + 0.0005, False), (k * period + 0.0045, True)):
                same = timer.same_refresh(measured, period)
                if k >= 20:
                    self.assertEqual(same, second)
                if not same:
                    ests.append((k, timer.update(measured, period)))
        steps = [b - a for (k, a), (_, b) in zip(ests, ests[1:]) if k >= 20]
        self.assertLess(max(abs(s - period) for s in steps), 0.0006)

    def test_learned_doubling_survives_reset(self):
        """再生し直しても (reset)、入れ替えが 2 回通る画面だと覚えていて、最初の書き換えから数え分ける。"""
        period = 1 / 60
        timer = VsyncTimer()
        for k in range(30):
            for measured in (k * period, k * period + 0.004):
                if not timer.same_refresh(measured, period):
                    timer.update(measured, period)
        timer.reset()
        t = 10.0
        self.assertFalse(timer.same_refresh(t, period))
        timer.update(t, period)
        self.assertTrue(timer.same_refresh(t + 0.004, period))
        self.assertFalse(timer.same_refresh(t + period, period))

    def test_late_notice_is_not_same_refresh(self):
        """知らせが遅れても (144Hz で 0〜9 ms)、次の書き換えの知らせは同じ書き換えとみなさない。"""
        rnd = random.Random(2)
        period = 1 / 144
        timer = VsyncTimer()
        for k in range(300):
            measured = k * period + rnd.choice((0.0, 0.009, rnd.uniform(0, 0.009)))
            self.assertFalse(timer.same_refresh(measured, period))
            timer.update(measured, period)
        self.assertFalse(timer.doubled)


class VsyncLockTest(unittest.TestCase):
    def test_advances_one_step_per_swap(self):
        lock = VsyncLock(1 / 144, 1.0)
        lock.rebase(10.0, 2.0)
        for k in range(1, 100):
            self.assertAlmostEqual(lock.advance(10.0 + k / 144), 2.0 + k / 144)

    def test_late_notice_does_not_skip(self):
        """知らせが 1 回分近く遅れても、書き換えの回数で進めるのでコマは飛ばない。"""
        lock = VsyncLock(1 / 144, 1.0)
        lock.rebase(0.0, 0.0)
        times = [lock.advance(k / 144 + (0.006 if k % 7 == 0 else 0.0)) for k in range(1, 200)]
        frames = [round(t * 144) for t in times]
        self.assertEqual(frames, list(range(1, 200)))

    def test_catches_up_after_real_stall(self):
        """本当に描画が止まって書き換えを何回も飛ばしたら、壁時計に追いつく。"""
        lock = VsyncLock(1 / 144, 1.0)
        lock.rebase(0.0, 0.0)
        for k in range(1, 10):
            lock.advance(k / 144)
        t = lock.advance(20 / 144)                         # 10〜19 回目の書き換えを飛ばした
        self.assertGreaterEqual(round(t * 144), 19)


if __name__ == "__main__":
    unittest.main()
