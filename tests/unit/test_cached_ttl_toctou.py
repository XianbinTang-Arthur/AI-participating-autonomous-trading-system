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
from types import SimpleNamespace
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
    # B1 测试要求 self.logger 可用（stale_fallback 里 log_event 会用到；
    # 即使测试路径 try/except 保护了，给一个真实 logger 也让 log 可见）
    import logging
    service.logger = logging.getLogger("test_cached_ttl")
    # _reentrant_guard 是类级别 threading.local，不用注入
    return service


class _RuntimeSettings(SimpleNamespace):
    def expanded_allowed_symbols(self) -> tuple[str, ...]:
        return ("BTC-USDT-SWAP",)


def _make_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        settings=_RuntimeSettings(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            smart_arbitrage_margin_short_enabled=False,
            smart_arbitrage_negative_basis_mode="disabled",
            smart_arbitrage_margin_short_spot_margin_mode="cross",
        )
    )


class TestCachedTtlTocTouRaceFix(unittest.TestCase):
    """Leader 完成 loader 后，所有 follower 必须读到缓存，不能兜底重跑 loader。"""

    def test_operator_query_instances_share_inflight_for_same_runtime(self) -> None:
        runtime = _make_runtime()
        with patch(
            "aats.services.operator.query_service.StrategyProfileControlService",
            lambda _runtime: SimpleNamespace(),
        ):
            first = OperatorQueryService(runtime)
            second = OperatorQueryService(runtime)
        self.assertIs(first._ttl_cache, second._ttl_cache)
        self.assertIs(first._cache_lock, second._cache_lock)
        self.assertIs(first._inflight, second._inflight)

        leader_entered = threading.Event()
        leader_release = threading.Event()
        loader_call_count = 0
        counter_lock = threading.Lock()
        results: list[str] = []

        def slow_loader() -> str:
            nonlocal loader_call_count
            with counter_lock:
                loader_call_count += 1
                is_leader = loader_call_count == 1
            if is_leader:
                leader_entered.set()
                leader_release.wait(timeout=3.0)
            return "shared-value"

        def run(service: OperatorQueryService) -> None:
            results.append(service._cached_ttl("shared-runtime-key", 60, slow_loader))

        leader = threading.Thread(target=run, args=(first,))
        follower = threading.Thread(target=run, args=(second,))
        leader.start()
        self.assertTrue(leader_entered.wait(timeout=2.0))
        follower.start()
        time.sleep(0.05)
        leader_release.set()
        leader.join(timeout=3.0)
        follower.join(timeout=3.0)

        self.assertFalse(leader.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertEqual(loader_call_count, 1)
        self.assertEqual(results, ["shared-value", "shared-value"])

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

    2026-04-21 更新：B1 引入负缓存后，leader 抛异常时错误会被缓存 2s。
    后续 follower 在负缓存窗口内命中会立即 re-raise 同一异常（而不是自己
    再跑 loader）—— 这是**更 system-friendly 的行为**（防止 N follower
    同时打爆上游）。本测试的核心不变性仍是"follower 不卡住"，但预期行为
    从"follower 自跑 loader 并成功"改成"follower 立刻 raise 负缓存里的错误"。
    """

    def test_cached_ttl_leader_exception_wakes_followers_and_negative_cache_fires(self) -> None:
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
            # 如果这条路径被触发，说明负缓存**没**生效（不对）
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
        leader.join(timeout=3.0)
        self.assertFalse(leader.is_alive())

        # leader 挂了：event.set()、_inflight 已 pop、_ttl_cache 里有负缓存
        # 起 follower —— 它命中负缓存、立即 re-raise，不跑 loader 也不卡
        follower = threading.Thread(target=worker, name="follower")
        follower.start()
        follower.join(timeout=2.0)
        self.assertFalse(
            follower.is_alive(),
            "follower 卡住超过 2s —— leader 抛异常后没唤醒 follower?",
        )

        # 关键不变性：loader 只在 leader 路径被调用一次（attempt == 1）
        self.assertEqual(
            attempt,
            1,
            f"期望 loader 只被调用一次（leader），但 attempt={attempt}。"
            "说明负缓存未生效 —— follower 又独立跑了一次 loader。",
        )
        # leader + follower 都 raise，且都是同一个异常
        self.assertEqual(len(errors), 2, f"期望 leader 和 follower 都 raise: errors={errors}")
        self.assertTrue(all(isinstance(e, RuntimeError) for e in errors))
        self.assertTrue(
            all(str(e) == "leader intentionally failed" for e in errors),
            "follower 应该 re-raise 负缓存里同一个异常实例，不同的错误消息说明 "
            "follower 自己跑了 loader",
        )
        # 没有成功 results（loader 第二次会返回 "follower-value"，但不应被调用）
        self.assertEqual(results, [])


class TestCachedTtlNegativeCaching(unittest.TestCase):
    """2026-04-21 B1 · Grafana 负缓存 + stale fallback 测试。

    目的：当 loader 抛 Exception 时，短 TTL 窗口内缓存错误，防止 follower
    都独立重试打爆上游（OKX / PG）。同时：follower 超时有过期 cache 时
    优先返回 stale，不重新跑 loader。
    """

    def test_loader_exception_cached_and_reraised(self) -> None:
        """loader 第一次抛异常 → 负缓存 → 第二次命中负缓存立即 raise，不再
        调 loader。"""
        service = _make_service()
        call_count = 0

        def failing_loader():
            nonlocal call_count
            call_count += 1
            raise RuntimeError(f"boom {call_count}")

        # 第一次 call → loader 被跑 → raise RuntimeError(boom 1)
        with self.assertRaises(RuntimeError) as ctx1:
            service._cached_ttl("k", 60, failing_loader)
        self.assertEqual(str(ctx1.exception), "boom 1")
        self.assertEqual(call_count, 1)

        # 立即 再 call → 命中负缓存 → re-raise 同一个异常实例，loader NOT invoked
        with self.assertRaises(RuntimeError) as ctx2:
            service._cached_ttl("k", 60, failing_loader)
        self.assertEqual(str(ctx2.exception), "boom 1")  # 同一个原始异常
        self.assertEqual(
            call_count,
            1,
            "负缓存未生效 —— loader 在 _NEGATIVE_CACHE_SECONDS 内被第二次调用，"
            "说明上游会被 N 个 follower 重试打穿",
        )

    def test_negative_cache_expires_and_retries(self) -> None:
        """负缓存 TTL 到期后，下次 call 重新跑 loader。"""
        service = _make_service()
        # 为了测试快，把负缓存 TTL 改短（monkeypatch）
        service._NEGATIVE_CACHE_SECONDS = 1  # 1s 测试窗口

        call_count = 0

        def flaky_loader():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError(f"fail {call_count}")
            return "ok"

        with self.assertRaises(RuntimeError):
            service._cached_ttl("k", 60, flaky_loader)  # call 1 fails
        self.assertEqual(call_count, 1)

        # 马上再 call：命中负缓存
        with self.assertRaises(RuntimeError):
            service._cached_ttl("k", 60, flaky_loader)  # cached, loader NOT called
        self.assertEqual(call_count, 1, "负缓存应该命中")

        # 等 1.1s 让负缓存过期
        time.sleep(1.1)

        # 再 call: 负缓存过期 → loader 被重新调用（失败）
        with self.assertRaises(RuntimeError):
            service._cached_ttl("k", 60, flaky_loader)
        self.assertEqual(call_count, 2, "负缓存过期后应该 retry")

        # 再等 1.1s 让新负缓存过期 + 这次 loader 成功
        time.sleep(1.1)
        result = service._cached_ttl("k", 60, flaky_loader)
        self.assertEqual(result, "ok")
        self.assertEqual(call_count, 3)

        # 成功后，后续命中正缓存，不再调 loader
        result2 = service._cached_ttl("k", 60, flaky_loader)
        self.assertEqual(result2, "ok")
        self.assertEqual(call_count, 3, "成功后应覆盖负缓存，正缓存命中不 re-call")

    def test_successful_loader_overwrites_cached_error(self) -> None:
        """loader 此前失败（负缓存存在）→ 同 key 再次成功 → 正缓存覆盖负缓存。"""
        service = _make_service()
        service._NEGATIVE_CACHE_SECONDS = 10  # 让负缓存还没过期

        calls: list[str] = []

        def sometimes_fails():
            if not calls:
                calls.append("fail")
                raise RuntimeError("first call fails")
            calls.append("ok")
            return "success-value"

        # call 1: fails, cached as error
        with self.assertRaises(RuntimeError):
            service._cached_ttl("k", 60, sometimes_fails)

        # 立即手动清负缓存模拟"上游恢复"（实际业务代码通过 _invalidate_cache
        # 触发）；这里为了测试"成功后覆盖"的幂等性，直接清再调
        with service._cache_lock:
            service._ttl_cache.pop("k", None)

        # call 2: succeeds, writes positive cache
        result = service._cached_ttl("k", 60, sometimes_fails)
        self.assertEqual(result, "success-value")

        # call 3: hits positive cache, no loader call
        result3 = service._cached_ttl("k", 60, sometimes_fails)
        self.assertEqual(result3, "success-value")
        self.assertEqual(
            calls,
            ["fail", "ok"],
            "第 3 次应命中正缓存不再调 loader",
        )

    def test_stale_fallback_on_follower_timeout(self) -> None:
        """★ 关键 system-friendly 属性 ★：leader 超时、follower 有过期 cache
        时，返回 stale value 不再自跑 loader —— 避免 N follower 打爆上游。

        场景：
          1. 先填一个过期 cache（模拟 "之前的 leader 成功过、但 TTL 到了"）
          2. leader 线程在 loader 里阻塞足够久让 follower 超时
          3. follower 等 1s（monkey-patched SINGLEFLIGHT_WAIT）超时
          4. follower 看到有过期 value → 返回 stale，不跑 loader
        """
        service = _make_service()
        # 把 follower 等待窗口缩到 1s 方便测试
        service._SINGLEFLIGHT_WAIT_SECONDS = 1

        # 1. 预先种一个过期的 cache entry（ttl 已过）
        from datetime import datetime, timezone, timedelta
        expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        service._ttl_cache["k"] = (expired_at, "stale-value")

        # 2. leader 在 loader 里 block 3s（大于 follower 超时 1s）
        leader_entered = threading.Event()
        leader_release = threading.Event()
        loader_call_count = [0]

        def slow_loader():
            loader_call_count[0] += 1
            if loader_call_count[0] == 1:
                leader_entered.set()
                leader_release.wait(timeout=4)
                return "fresh-value"
            # follower 路径不应该到达这里
            return "follower-should-not-call-this"

        results: list[str] = []
        errors: list[BaseException] = []

        def run_leader():
            try:
                results.append(service._cached_ttl("k", 60, slow_loader))
            except BaseException as e:
                errors.append(e)

        def run_follower():
            try:
                results.append(service._cached_ttl("k", 60, slow_loader))
            except BaseException as e:
                errors.append(e)

        leader = threading.Thread(target=run_leader)
        leader.start()
        self.assertTrue(leader_entered.wait(timeout=2), "leader 没进 loader")

        # follower 跟进
        follower = threading.Thread(target=run_follower)
        follower.start()

        # 等 follower 超时（1s）+ 一点 buffer
        follower.join(timeout=3)
        self.assertFalse(follower.is_alive(), "follower 超时未归")

        # ★ 断言：follower 返回了 stale value，loader 没再被 follower 调一次
        # （只有 leader 那一次）
        self.assertEqual(
            loader_call_count[0],
            1,
            f"CRITICAL: follower 超时后自己又跑了 loader，共 {loader_call_count[0]} 次。"
            f"stale fallback 未生效 —— N follower 同时 loader 会打爆上游。",
        )
        # follower 应该已经拿到 stale
        self.assertIn("stale-value", results)

        # 释放 leader 让它跑完
        leader_release.set()
        leader.join(timeout=3)
        self.assertEqual(
            loader_call_count[0],
            1,
            "leader 应该在被释放后走原路径不再多调",
        )

    def test_stale_fallback_does_not_fire_for_cached_error(self) -> None:
        """stale fallback 只对正常旧值生效；_CachedError 即使是 "旧的"
        也不能当 stale value 返回 —— 用户要么拿到 re-raise、要么走 loader。"""
        service = _make_service()
        service._SINGLEFLIGHT_WAIT_SECONDS = 1
        service._NEGATIVE_CACHE_SECONDS = 5

        from aats.services.operator.query_service import _CachedError
        from datetime import datetime, timezone, timedelta
        # 种一个 EXPIRED _CachedError entry（时间戳已过）
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        service._ttl_cache["k"] = (past, _CachedError(RuntimeError("old error")))

        # 起一个 leader 在 loader 里 block
        leader_entered = threading.Event()
        leader_release = threading.Event()
        loader_calls: list[str] = []

        def slow_loader():
            loader_calls.append("called")
            if len(loader_calls) == 1:
                leader_entered.set()
                leader_release.wait(timeout=4)
                return "fresh"
            return "second-call-value"

        results: list[str] = []
        errors: list[BaseException] = []

        def run(target_list, err_list):
            try:
                target_list.append(service._cached_ttl("k", 60, slow_loader))
            except BaseException as e:
                err_list.append(e)

        leader = threading.Thread(target=run, args=(results, errors))
        leader.start()
        self.assertTrue(leader_entered.wait(timeout=2))

        follower = threading.Thread(target=run, args=(results, errors))
        follower.start()
        follower.join(timeout=3)
        self.assertFalse(follower.is_alive())

        # follower 路径：过期 _CachedError 不作为 stale value → 跑 loader
        # （这里 loader 是 block 的，follower 自跑的 loader 走第二次 slow_loader 路径）
        self.assertEqual(
            len(loader_calls),
            2,
            "过期 _CachedError 不应该被当 stale value，follower 必须跑自己的 loader",
        )
        self.assertIn("second-call-value", results)

        # 清理：释放 leader
        leader_release.set()
        leader.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
