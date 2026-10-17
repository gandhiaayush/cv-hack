"""Concurrency cap + queue for model calls.

Agents are cheap threads that mostly wait on the model, so the limit is on in-flight LLM requests, not on
agents. That keeps us under Crusoe's request limit, lets any number of agents be queued, and cannot deadlock
when agents spawn agents (a parent waiting on its children holds no slot).
MAX_CONCURRENT (env, default 10) is the cap."""
import os
import threading
import time
from contextlib import contextmanager

from . import store


class Scheduler:
    def __init__(self, n: int):
        self.n = n
        self.sem = threading.BoundedSemaphore(n)
        self.lock = threading.Lock()
        self.running = self.waiting = self.peak = 0

    @contextmanager
    def slot(self, run: str, agent: str):
        with self.lock:
            self.waiting += 1
        t0 = time.time()
        self.sem.acquire()
        with self.lock:
            self.waiting -= 1
            self.running += 1
            self.peak = max(self.peak, self.running)
            snap = {"running": self.running, "waiting": self.waiting, "cap": self.n}
        if time.time() - t0 > 1.0:  # only log real queueing, not every call
            store.log(run, agent, None, "queue", {**snap, "waited_s": round(time.time() - t0, 1)})
        try:
            yield
        finally:
            with self.lock:
                self.running -= 1
            self.sem.release()


SCHED = Scheduler(int(os.environ.get("MAX_CONCURRENT", "10")))
