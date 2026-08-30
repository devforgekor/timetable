#!/usr/bin/env python3
# Status: production
# Path: imported by — scripts/tests/*, temp test scripts
"""Shared-queue work distribution across independent model pods.

Pattern:
  Place items in a thread-safe ``queue.Queue``.  Each worker thread (bound to a
  specific port / llama-server process) pulls the next item, processes it, and
  saves incrementally.  Workers exit when the queue is empty.

  This is NOT ``ThreadPoolExecutor`` — workers target different ports, not
  different slots of the same server.  That avoids the upstream slot-deadlock
  bugs (#20906, #22160).

Usage::

    from lib.work_steal import WorkStealer

    def process(port, item, item_timeout):
        raw = call_model(port, item["prompt"], timeout=item_timeout)
        return {"ok": True, "data": raw}

    stealer = WorkStealer(ports=[8082, 8083], item_timeout=180)
    results = stealer.run(items=[...], process_fn=process,
                          save_path="/tmp/results.json")

    print(f"Done: {stealer.ok_count}/{stealer.total}")
"""

import json
import os
import queue
import threading
import time
from typing import Any, Callable, List, Optional


class WorkStealer:
    """Distribute work items across multiple model pods via shared queue.

    Thread-safe.  Each worker is a non-daemon thread bound to one ``port``.
    Workers exit cleanly when the shared queue is empty or ``shutdown``
    is set (graceful on Ctrl+C).

    Attributes:
        ports:          List of ports to distribute work across.
        item_timeout:   Seconds per item (passed as kwarg to process_fn).
        total:          Number of items submitted.
        ok_count:       Number of items completed without error.
        skip_count:     Number of items skipped (error or timeout).
        results:        Accumulated result dicts (thread-safe access).
        elapsed:        Wall-clock seconds of the last ``run()``.
        per_port:       Dict mapping port -> {ok, skip, total}.
    """

    def __init__(self, ports: List[int], item_timeout: int = 180):
        assert ports, "At least one port required"
        self.ports = list(ports)
        self.item_timeout = item_timeout
        self.total = 0
        self.ok_count = 0
        self.skip_count = 0
        self.results: List[dict] = []
        self.elapsed = 0.0
        self.per_port: dict = {p: {"ok": 0, "skip": 0, "total": 0} for p in ports}

        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._save_path: Optional[str] = None
        self._progress_cb: Optional[Callable] = None
        self._shutdown = threading.Event()

    def shutdown(self):
        """Signal all workers to exit after current item."""
        self._shutdown.set()

    def run(
        self,
        items: List[Any],
        process_fn: Callable[[int, Any, int], dict],
        save_path: Optional[str] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[dict]:
        """Execute ``process_fn(port, item, item_timeout)`` per item.

        Args:
            items:      Work items (any type).  Each is passed verbatim to
                        ``process_fn``.
            process_fn: ``fn(port, item, item_timeout) -> dict``.
                        Return dict **must** include ``"ok": bool``.
                        Other keys are caller-defined.
            save_path:  Optional path for incremental JSON persistence.
                        Uses atomic write (``.tmp`` + ``os.replace``).
            progress_cb:  Optional ``fn(done_count, total)`` called after each
                        item completes.

        Returns:
            Flat list of result dicts (one per item).
        """
        for item in items:
            self._queue.put(item)
        self.total = len(items)
        self._save_path = save_path
        self._progress_cb = progress_cb

        t0 = time.time()
        threads = []
        for port in self.ports:
            t = threading.Thread(
                target=self._worker,
                args=(port, process_fn),
                daemon=False,
            )
            t.start()
            threads.append(t)

        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            self._shutdown.set()
            for t in threads:
                t.join(timeout=2)

        self.elapsed = time.time() - t0
        if save_path:
            self._save(save_path)
        return list(self.results)

    # ── internals ──────────────────────────────────────────────────────────

    def _worker(self, port: int, process_fn: Callable):
        while not self._shutdown.is_set():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return

            t0 = time.time()
            try:
                r = process_fn(port, item, self.item_timeout)
                if not isinstance(r, dict):
                    r = {"ok": False, "error": f"non-dict return: {type(r).__name__}"}
            except Exception as exc:
                r = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

            r.setdefault("ok", False)
            r.setdefault("port", port)
            r.setdefault("elapsed", round(time.time() - t0, 1))

            with self._lock:
                self.results.append(r)
                pp = self.per_port[port]
                pp["total"] += 1
                if r["ok"]:
                    self.ok_count += 1
                    pp["ok"] += 1
                else:
                    self.skip_count += 1
                    pp["skip"] += 1

            # Save outside lock — file I/O should not block other workers
            done = self.ok_count + self.skip_count
            if self._save_path and done % 5 == 0:
                self._save(self._save_path)

            if self._progress_cb:
                self._progress_cb(done, self.total)

    def _save(self, path: str):
        """Atomic write — avoids partial-file reads by other processes."""
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {
                    "results": self.results,
                    "ok": self.ok_count,
                    "skip": self.skip_count,
                    "total": self.total,
                    "elapsed": round(self.elapsed, 1),
                    "per_port": self.per_port,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        os.replace(tmp, path)

    def worker_report(self) -> str:
        """One-line summary string for logging."""
        parts = [f"ok={self.ok_count} skip={self.skip_count}/{self.total}"]
        for p in self.ports:
            pp = self.per_port[p]
            parts.append(f":{p}={pp['ok']}ok/{pp['skip']}skip")
        parts.append(f"{self.elapsed:.0f}s")
        return " ".join(parts)
