"""Stop-current-anime-download act tool: a Qt-free forwarding shim.

The tool carries no download authority.  Its injected Host closure validates the
live request identity and emits a typed event for the UI-side controller.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from agent_tools.function_tools.screen.schema import ScreenToolError

RequestAnimeCancel = Callable[[str], dict[str, Any]]


CANCEL_ANIME_DOWNLOAD_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "cancel_anime_download",
    "strict": True,
    "description": (
        "停止当前正在下载的动漫。仅当用户明确表示不要继续下载、要停止或取消当前动漫下载时调用；"
        "工具提交停止请求后，你应简短确认正在停止，不要声称文件已经删除完成。"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}


class CancelAnimeDownloadTool:
    """Forward one cancel request to the injected Host-owned action surface."""

    name = "cancel_anime_download"

    def __init__(self, request_cancel: RequestAnimeCancel) -> None:
        self._request_cancel = request_cancel
        # Capability generation and execution happen on the same run_turn
        # producer thread.  A thread-local, one-shot offer keeps concurrent
        # turns isolated without widening the generic registry/runtime API.
        self._offered = threading.local()

    def schema(self) -> dict[str, Any]:
        return CANCEL_ANIME_DOWNLOAD_SCHEMA

    def clear_offer(self) -> None:
        """Clear this thread's prior capability identity before every offer."""
        self._offered.request_id = None

    def bind_offer(self, request_id: str) -> None:
        """Bind one immutable request identity to this thread's next call."""
        self.clear_offer()
        value = str(request_id or "")
        if value:
            self._offered.request_id = value

    def run(self) -> dict[str, Any]:
        # Pop before ANY live-state check or side effect.  Forced, duplicate,
        # cross-thread and retry calls therefore cannot reuse an old offer.
        expected_request_id = getattr(self._offered, "request_id", None)
        self._offered.request_id = None
        if not expected_request_id:
            raise ScreenToolError(
                "ANIME_CANCEL_REQUEST_STALE",
                "停止请求已过期，请根据当前下载状态重新发起。",
            )
        return self._request_cancel(str(expected_request_id))
