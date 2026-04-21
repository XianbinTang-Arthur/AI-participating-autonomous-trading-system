"""2026-04-21 锚点测试：_cached / _cached_ttl 的 event.set() 与 cache 写入
必须放在同一个 critical section，防止 TOCTOU 惊群放大。

## 背景

旧实现（被此次修复推翻）：

```python
try:
    value = loader()
finally:
    with self._cache_lock:
        self._inflight.pop(key, None)
        event.set()           # ← 先唤醒 follower
    # lock 释放
with self._cache_lock:        # ← 再获锁写 cache
    self._ttl_cache[key] = ...
```

问题：follower 被 event.set() 唤醒后，与 leader 竞争 _cache_lock。
如果 follower 抢到锁先，发现 _ttl_cache 为空，会回落到
``return loader()`` 兜底路径 —— 原本 1 次 loader 变成 N+1 次，
singleflight 名存实亡、UI 冷启动退化回 parallel_fetch 惊群。

## 本测试的作用

- 用 SlowEvent 人为拉宽 event.set() → lock re-acquire 之间的窗口
- 启动多个 follower 抢在 leader 写 cache 之前拿到 _cache_lock
- 如果没修复：loader 会被调用 N 次（N = follower 数量 + 1）
- 如果修复了：loader 只会被调用 1 次（follower 拿到 lock 时 cache 已就位）

即使未来有人把修复 "重构没了"（比如看到 finally 觉得"更优雅"把
event.set() 挪回去），这个测试会立即失败。
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from aats.services.operator.query_service import OperatorQueryService


class _SlowEvent(threading.Event):
    """Event 子类：set() 之后 sleep 50ms，人为拉宽 race 窗口。

    这样一定能让被唤醒的 follower 抢先拿到 _cache_lock，制造
    "事件已发、缓存尚空" 的状态。正确的代码必须让 follower 在这
    种状态下依然读到缓存（因为缓存写入和 event.set() 在同一个
    critical section 里）。
    """

    def set(self) -> None:  # type: ignore[override]
        super().set()
        time.sleep(0.05)


def _make_service() -> OperatorQueryService:
    """绕过 __init__ 构造一个最小可测的 OperatorQueryService。

    只注入 _cached / _cached_ttl 需要的属性；不起真实 runtime。
    """
    service = OperatorQueryService.__new__(OperatorQueryService)
    service._cache = {}
    service._ttl_cache = {}
    service._inflight = {}
    service._cache_lock = threading.RLock()
    # _reentrant_guard 是类级别 threading.local，不用注入
    return service


class TestCachedTtlTocTouRaceFix(unittest.TestCase):
    """Leader 完成 loader 后，所有 follower 必须读到缓存，不能兜底重跑 loader。"""

    def _run_contention_test(self, *, cache_method_name: str, ttl_seconds: int) -> int:
        """返回 loader 被调用次数。修复后应为 1。"""
        service = _make_service()

        call_count = 0
        counter_lock = threading.Lock()
        leader_in_loader = threading.Event()
        leader_release = threading.Event()

        def slow_loader():
            nonlocal call_count
            with counter_lock:
                call_count += 1
                is_leader = call_count == 1
            if is_leader:
                # Leader：告知测试 "我进 loader 了"，等测试放我出去
                leader_in_loader.set()
                leader_release.wait(timeout=3.0)
            return "sentinel_value"

        results: list[str] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def worker():
            try:
                if cache_method_name == "_cached":
                    value = service._cached("race-key", slow_loader)
                else:
                    value = service._cached_ttl("race-key", ttl_seconds, slow_loader)
                with result_lock:
                    results.append(value)
            except BaseException as exc:  # noqa: BLE001
                with result_lock:
                    errors.append(exc)

        # Start leader
        leader = threading.Thread(target=worker, name="leader")
        leader.start()
        self.assertTrue(
            leader_in_loader.wait(timeout=2.0),
            "leader 没能进入 loader 内；可能 __import__('threading').Event "
            "没被 patch，或 _cached 实现路径变了",
        )

        # 此时 leader 已在 loader 内阻塞，_inflight['race-key'] 已登记
        # 启动 follower，它们会发现 inflight event 并进入 wait
        num_followers = 5
        followers = [
            threading.Thread(target=worker, name=f"follower-{i}")
            for i in range(num_followers)
        ]
        for f in followers:
            f.start()
        # 给 follower 足够时间到达 wait_for.wait()
        time.sleep(0.1)

        # 释放 leader —— leader 完成 loader，进 finally，
        # 触发 event.set() (SlowEvent 会 sleep 50ms 让 follower 抢占锁)
        leader_release.set()

        # 等所有 thread 结束
        for t in [leader] + followers:
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive(), f"{t.name} 超时未结束 —— 死锁?")

        self.assertEqual(errors, [], f"unexpected errors: {errors}")
        self.assertEqual(
            len(results),
            1 + num_followers,
            f"不是所有 worker 都返回了: results={results}",
        )
        self.assertTrue(
            all(r == "sentinel_value" for r in results),
            f"worker 拿到了错误的值: {results}",
        )
        return call_count

    def test_cached_no_thundering_herd_under_event_set_race(self) -> None:
        """_cached：leader + 5 follower，总 loader 调用 == 1。"""
        with patch("threading.Event", _SlowEvent):
            call_count = self._run_contention_test(
                cache_method_name="_cached",
                ttl_seconds=0,
            )
        self.assertEqual(
            call_count,
            1,
            f"_cached TOCTOU 惊群放大：loader 被调了 {call_count} 次 "
            f"(预期 1 —— 只有 leader 应该跑 loader，所有 follower 读缓存)。"
            f"说明 event.set() 与 cache 写入不在同一个 critical section。",
        )

    def test_cached_ttl_no_thundering_herd_under_event_set_race(self) -> None:
        """_cached_ttl：leader + 5 follower，总 loader 调用 == 1。"""
        with patch("threading.Event", _SlowEvent):
            call_count = self._run_contention_test(
                cache_method_name="_cached_ttl",
                ttl_seconds=60,
            )
        self.assertEqual(
            call_count,
            1,
            f"_cached_ttl TOCTOU 惊群放大：loader 被调了 {call_count} 次 "
            f"(预期 1 —— 只有 leader 应该跑 loader，所有 follower 读缓存)。"
            f"说明 event.set() 与 _ttl_cache 写入不在同一个 critical section。",
        )


class TestCachedTtlLeaderFailureWakesFollowers(unittest.TestCase):
    """Leader 抛异常时，follower 必须被唤醒（不能一直卡在 wait）。

    修复版的代码在 except 分支里显式调 event.set()；这个测试防止
    未来有人忘了这条 except 分支导致 follower 死等 25s。
    """

    def test_cached_ttl_leader_exception_wakes_followers(self) -> None:
        service = _make_service()

        attempt = 0
        attempt_lock = threading.Lock()
        leader_ready = threading.Event()

        def loader():
            nonlocal attempt
            with attempt_lock:
                attempt += 1
                my_attempt = attempt
            if my_attempt == 1:
                # Leader：告知测试并立即抛异常
                leader_ready.set()
                raise RuntimeError("leader intentionally failed")
            return "follower-value"

        errors: list[BaseException] = []
        results: list[str] = []
        result_lock = threading.Lock()

        def worker():
            try:
                value = service._cached_ttl("fail-key", 60, loader)
                with result_lock:
                    results.append(value)
            except BaseException as exc:  # noqa: BLE001
                with result_lock:
                    errors.append(exc)

        leader = threading.Thread(target=worker, name="leader")
        leader.start()
        self.assertTrue(leader_ready.wait(timeout=2.0))
        # leader 已经抛异常，event.set() 已经发了 —— 这个 Barrier 给的时间让
        # finally 在 thread 结束前完成
        leader.join(timeout=3.0)
        self.assertFalse(leader.is_alive())

        # leader 挂了，但 _inflight 已经 pop，event 已经 set
        # 此时起 follower —— 它们应该走 "leader 没 cache" fallback，
        # 不应该死等 25s
        follower = threading.Thread(target=worker, name="follower")
        follower.start()
        # follower 应该在 < 1s 内完成（走 fallback 路径：event 已 set，
        # _inflight 无 entry → 自己当新 leader → 跑 loader → 返回）
        follower.join(timeout=2.0)
        self.assertFalse(
            follower.is_alive(),
            "follower 卡住超过 2s —— leader 抛异常后没唤醒 follower?",
        )
        self.assertEqual(len(errors), 1, f"期望仅 leader 抛错: errors={errors}")
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(results, ["follower-value"])


if __name__ == "__main__":
    unittest.main()
