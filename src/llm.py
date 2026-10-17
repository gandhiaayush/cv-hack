"""Thin Crusoe Managed Inference client (OpenAI-compatible endpoint)."""
import os
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
        )
    return _client


def list_models() -> list[str]:
    return [m.id for m in client().models.list().data]


def chat(messages, model, tools=None, **kw):
    """One chat completion. Returns the assistant message."""
    args = dict(model=model, messages=messages, **kw)
    if tools:
        args["tools"] = tools
    return client().chat.completions.create(**args).choices[0].message
