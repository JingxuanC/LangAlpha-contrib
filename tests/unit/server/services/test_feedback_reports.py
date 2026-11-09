"""The races a thumbs-down's report is guarded against: a rating removed or
changed while its report is on its way takes that report back, and a step the
platform did not take leaves the rating as it was."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services import feedback
from src.server.services.reports import PlatformResult, ThumbsDown

ACCEPTED = PlatformResult("accepted")


def _row(**overrides):
    row = {
        "conversation_feedback_id": "f1",
        "conversation_response_id": "r1",
        "user_id": "u1",
        "rating": "thumbs_down",
        "report_id": "rep1",
        "reported_at": None,
        "consent_human_review": False,
        "issue_categories": [],
        "comment": None,
    }
    row.update(overrides)
    return row


def _thumbs(row):
    return ThumbsDown.from_row(row, "t1", 0)


@pytest.mark.asyncio
async def test_a_report_landing_after_the_rating_went_is_withdrawn_and_not_called_sent():
    row = _row()
    with patch.object(feedback, "send_thumbs_down", AsyncMock(return_value=ACCEPTED)), \
         patch.object(feedback, "mark_feedback_reported", AsyncMock(return_value=None)), \
         patch.object(feedback, "withdraw_thumbs_down", AsyncMock(return_value=ACCEPTED)) as withdraw:
        sent, _ = await feedback._send_report(row, _thumbs(row), None)
    assert sent.status == "closed"
    withdraw.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_step_recorded_after_the_rating_went_is_withdrawn_and_refused():
    row = _row()
    with patch.object(feedback, "withdraw_thumbs_down", AsyncMock(return_value=ACCEPTED)) as withdraw:
        with pytest.raises(feedback.ReportClosed):
            await feedback._kept(AsyncMock(return_value=None)(), _thumbs(row))
    withdraw.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_refused_withdrawal_keeps_the_rating():
    with patch.object(feedback, "reports_enabled", return_value=True), \
         patch.object(feedback, "withdraw_thumbs_down", AsyncMock(return_value=PlatformResult("rate_limited", 5))):
        with pytest.raises(feedback.ReportRefused) as refused:
            await feedback._withdraw_report(_row(), "t1", 0)
    assert refused.value.result.status == "rate_limited"


@pytest.mark.asyncio
async def test_with_reports_off_a_sent_report_cannot_be_dropped_but_an_unsent_one_can():
    withdraw = AsyncMock()
    with patch.object(feedback, "reports_enabled", return_value=False), \
         patch.object(feedback, "withdraw_thumbs_down", withdraw):
        with pytest.raises(feedback.ReportRefused):
            await feedback._withdraw_report(_row(reported_at="2026-11-09"), "t1", 0)
        await feedback._withdraw_report(_row(), "t1", 0)
    withdraw.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_review_asked_for_on_a_resend_is_recorded_only_once_the_platform_takes_it():
    prior = _row(reported_at="2026-11-09")
    upsert = AsyncMock(return_value=prior)
    with patch.object(feedback, "reports_enabled", return_value=True), \
         patch.object(feedback, "get_feedback_for_turn", AsyncMock(return_value=prior)), \
         patch.object(feedback, "upsert_feedback", upsert), \
         patch.object(feedback, "send_thumbs_down", AsyncMock(return_value=PlatformResult("duplicate"))), \
         patch.object(feedback, "mark_feedback_reported", AsyncMock(return_value=prior)), \
         patch.object(feedback, "request_thumbs_review", AsyncMock(return_value=PlatformResult("unavailable"))), \
         patch.object(feedback, "request_feedback_review", AsyncMock()) as record_review:
        row, status = await feedback.submit(
            "t1", 0, "u1", rating="thumbs_down", issue_categories=[], comment=None,
            consent_human_review=True, client=None,
        )
    assert upsert.await_args.kwargs["consent_human_review"] is False
    record_review.assert_not_called()
    assert status == "unavailable" and row["consent_human_review"] is False


@pytest.mark.asyncio
async def test_a_take_back_the_platform_refuses_is_logged_by_report_id(caplog):
    row = _row()
    with patch.object(feedback, "withdraw_thumbs_down", AsyncMock(return_value=PlatformResult("unavailable"))):
        with pytest.raises(feedback.ReportClosed):
            await feedback._kept(AsyncMock(return_value=None)(), _thumbs(row))
    assert "rep1" in caplog.text and "not withdrawn" in caplog.text
