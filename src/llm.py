"""Thin Crusoe Managed Inference client (OpenAI-compatible endpoint)."""
import os
import threading
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ["CRUSOE_API_KEY"],
            base_url=os.environ.get("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1"),
            max_retries=3,  # 429s are expected when the team shares one key; the SDK backs off
            timeout=300,  # the mother's long-context Nemotron calls take 1-2.5 min
        )
    return _client


def list_models() -> list[str]:
    return [m.id for m in client().models.list().data]


_last = threading.local()


def last_call() -> dict:
    """Model, token usage and latency of this thread's most recent chat() (for the run log)."""
    return getattr(_last, "info", {})


def chat(messages, model, tools=None, **kw):
    """One chat completion. Returns the assistant message."""
    args = dict(model=model, messages=messages, **kw)
    if tools:
        args["tools"] = tools
    t0 = time.time()
    r = client().chat.completions.create(**args)
    u = r.usage
    _last.info = {"model": r.model or model, "latency_s": round(time.time() - t0, 3),
                  "prompt_tokens": getattr(u, "prompt_tokens", None), "completion_tokens": getattr(u, "completion_tokens", None)}
    return r.choices[0].message
