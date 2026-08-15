import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


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
        stale: list[WebSocket] = []
        for websocket in clients:
            try:
                await websocket.send_json({"event": event, "payload": payload})
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(branch_id, websocket)


hub = BranchRealtimeHub()
