"""Live commentary endpoints. Mounted from server.py with install(app).

GET /api/narration         -> all lines so far
GET /api/narration/stream  -> SSE, resumable with Last-Event-ID"""
import asyncio
import threading

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from src.narrator import Narrator

router = APIRouter()
narrator = Narrator()


@router.get("/api/narration")
def lines():
    with narrator.lock:
        return list(narrator.lines)


@router.get("/api/narration/stream")
async def stream(request: Request):
    try:
        nxt = int(request.headers.get("last-event-id", "-1")) + 1
    except ValueError:
        nxt = 0

    async def gen():
        nonlocal nxt
        yield "retry: 1000\n\n"
        while not await request.is_disconnected():
            with narrator.lock:
                if narrator.lines and nxt > narrator.lines[-1]["id"] + 1:
                    nxt = 0  # log was reset by a new run
                new = [l for l in narrator.lines if l["id"] >= nxt]
            for l in new:
                import json
                yield f"id: {l['id']}\ndata: {json.dumps(l)}\n\n"
                nxt = l["id"] + 1
            if not new:
                yield ": ping\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def install(app):
    app.include_router(router)
    threading.Thread(target=narrator.run_forever, daemon=True).start()
