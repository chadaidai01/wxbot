# -*- coding: utf-8 -*-
"""运行时上下文（非上游文件）。

对应上游 Koishi 的 `ctx`：本移植只用到 database（→ store）、http/bots（→ platform）、
logger、定时器、baseDir。定时器用 threading 实现，提供 set_timeout/set_interval/clear_timer。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional


class _TimerHandle:
    __slots__ = ('timer',)

    def __init__(self, timer: threading.Timer):
        self.timer = timer

    def cancel(self) -> None:
        try:
            self.timer.cancel()
        except Exception:  # noqa: BLE001
            pass


class RuntimeContext:
    def __init__(self, store: Any, platform: Any, logger: Optional[logging.Logger] = None,
                 base_dir: str = '.', ready: bool = True):
        self.store = store
        self.platform = platform
        self.logger = logger or logging.getLogger('hdsi')
        self.base_dir = base_dir
        self.ready = ready
        self._timers: List[threading.Timer] = []
        self._lock = threading.Lock()

    def http_post(self, url: str, headers: Optional[Dict[str, str]] = None,
                  json_body: Optional[Dict[str, Any]] = None, timeout_ms: int = 60000,
                  stream: bool = False) -> Any:
        """上游 ctx.http.post 的默认实现（requests）。平台层可覆盖以复用既有 API 通道。"""
        import requests

        response = requests.post(url, headers=headers or {}, json=json_body,
                                 timeout=max(0.5, timeout_ms / 1000.0), stream=stream)
        if stream:
            return response
        try:
            return response.json()
        except ValueError:
            return {'__raw_text__': response.text, '__status__': response.status_code}

    # ---- 定时器 ----
    def set_timeout(self, fn: Callable[[], Any], milliseconds: float) -> _TimerHandle:
        def run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - 后台任务异常不能杀线程
                self.logger.exception('hdsi 定时任务失败')

        timer = threading.Timer(max(0.0, milliseconds / 1000.0), run)
        timer.daemon = True
        with self._lock:
            self._timers.append(timer)
        timer.start()
        return _TimerHandle(timer)

    def set_interval(self, fn: Callable[[], Any], milliseconds: float) -> _TimerHandle:
        interval = max(0.05, milliseconds / 1000.0)
        stopped = threading.Event()

        def run() -> None:
            while not stopped.wait(interval):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    self.logger.exception('hdsi 周期任务失败')

        thread = threading.Thread(target=run, name='hdsi-timer', daemon=True)
        thread.start()

        class _IntervalHandle:
            def cancel(self_inner) -> None:
                stopped.set()

        return _IntervalHandle()  # type: ignore[return-value]

    def clear_timer(self, handle: Any) -> None:
        if handle is None:
            return
        cancel = getattr(handle, 'cancel', None)
        if callable(cancel):
            cancel()

    def stop(self) -> None:
        with self._lock:
            timers = list(self._timers)
            self._timers = []
        for timer in timers:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass
