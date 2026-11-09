"""A 404 from the platform means "no such report" only when the platform says
so; any other 404 is a platform without the route, which a withdrawal must
not read as a report already gone."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.server.services import reports


def _client(status: int, body: dict) -> AsyncMock:
    client = AsyncMock()
    client.post.return_value = httpx.Response(status, json=body)
    return client


async def _withdraw_against(status: int, body: dict) -> reports.PlatformResult:
    with patch("src.server.utils.http_client.get_http_client", AsyncMock(return_value=_client(status, body))), \
         patch("src.server.dependencies.usage_limits.service_headers", return_value={}):
        return await reports._withdraw("rep1", "u1")


@pytest.mark.asyncio
async def test_a_report_the_platform_never_got_is_already_withdrawn():
    result = await _withdraw_against(404, {"detail": {"error": "report_not_found"}})
    assert result.status == "accepted"


@pytest.mark.asyncio
async def test_a_platform_without_the_route_leaves_the_withdrawal_undone():
    result = await _withdraw_against(404, {"detail": "Not Found"})
    assert result.status == "unavailable"
