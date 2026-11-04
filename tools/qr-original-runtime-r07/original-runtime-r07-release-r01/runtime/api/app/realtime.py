import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import HTTPException, WebSocket
from fastapi.encoders import jsonable_encoder


class BranchRealtimeHub:
    def __init__(self) -> None:
        self._clients: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, branch_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients[branch_id].add(websocket)

    async def disconnect(self, branch_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients[branch_id].discard(websocket)
            if not self._clients[branch_id]:
                self._clients.pop(branch_id, None)

    async def broadcast(self, branch_id: int, event: str, payload: dict[str, Any]) -> None:
        clients = list(self._clients.get(branch_id, set()))
        if not clients:
            return
        # HTTP responses already normalize dates, Decimal and UUID values. Use
        # the same boundary for sockets before classifying any client as stale.
        # An invalid payload must raise, not silently remove healthy clients.
        message = json.dumps(jsonable_encoder({"event": event, "payload": payload}),
                             separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        stale: list[WebSocket] = []
        for websocket in clients:
            try:
                authorize = getattr(websocket.state, "authorize", None)
                if authorize:
                    try:
                        await asyncio.to_thread(authorize)
                    except Exception as error:
                        unavailable = isinstance(error, HTTPException) and error.status_code == 503
                        await websocket.close(code=1013 if unavailable else 1008,
                                              reason="Auth temporarily unavailable" if unavailable else "Session expired")
                        stale.append(websocket)
                        continue
                await websocket.send_text(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(branch_id, websocket)


hub = BranchRealtimeHub()
