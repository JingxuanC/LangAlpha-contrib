"""Per-turn ratings, and the report a thumbs-down sends to the team, which
lives as long as the thumbs-down: added to while it stands, withdrawn when the
rating is removed or becomes a thumbs-up. Every rating is also kept as
feedback on its run in LangSmith (``sync_langsmith``, after the response).

Each step is recorded once the platform has it, and every write after a
platform call is guarded on the rating's report id, so a rating removed or
changed while its report was on its way takes that report back. A step the
platform did not take raises ``ReportRefused``; the rating then stays as it
was, so a retry finishes it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Optional

from src.observability.langsmith_feedback import mirror_rating, remove_rating, tracing_enabled
from src.server.database.conversation import get_feedback_for_thread, upsert_feedback
from src.server.database.conversation.feedback import (
    add_feedback_follow_up,
    delete_feedback_by_id,
    get_feedback_by_id,
    get_feedback_for_turn,
    mark_feedback_reported,
    request_feedback_review,
)
from src.server.models.report import ReportClient
from src.server.services.reports import (
    PlatformResult,
    ScrubUnavailable,
    ThumbsDown,
    reply_to_thumbs_down,
    reports_enabled,
    request_thumbs_review,
    send_thumbs_down,
    user_vault,
    withdraw_thumbs_down,
)

logger = logging.getLogger(__name__)

Row = dict[str, Any]


class ReportRefused(Exception):
    """The platform did not take a step; ``result`` says why."""

    def __init__(self, result: PlatformResult):
        super().__init__(result.status)
        self.result = result


class ReportClosed(Exception):
    """The rating no longer stands for the report the step was about."""


class NotFound(Exception):
    """No rating, report or turn to act on."""


def _has_report(row: Optional[Row]) -> bool:
    return bool(row and row["rating"] == "thumbs_down" and row.get("report_id") and reports_enabled())


async def _take_back(rating: ThumbsDown) -> None:
    """Withdraw a report the rating no longer stands for. One the platform
    refuses now stays in Slack with no rating to withdraw it through, so it is
    logged by id for the team to close."""
    done = await withdraw_thumbs_down(rating)
    if not done.ok:
        logger.warning(
            "Report %s outlived its rating and was not withdrawn (%s); close it in Slack",
            rating.report_id, done.status,
        )


async def _send_report(
    row: Row, rating: ThumbsDown, client: Optional[ReportClient]
) -> tuple[PlatformResult, Row]:
    """Send the report a thumbs-down's rating stands for, and note when the
    team has it. Returns what the platform said and the row as it now stands."""
    sent = await send_thumbs_down(rating, human_review=bool(row["consent_human_review"]), client=client)
    if not sent.ok:
        return sent, row
    marked = await mark_feedback_reported(str(row["conversation_feedback_id"]), rating.report_id)
    if marked is None:
        # The rating was removed, or changed to a thumbs-up, while this was
        # on its way, and that withdrawal found nothing yet to take back. The
        # team no longer has it, so the caller must not say it was sent.
        await _take_back(rating)
        return PlatformResult("closed"), row
    return sent, marked


async def _kept(update: Awaitable[Optional[Row]], rating: ThumbsDown) -> Row:
    """Record what the platform just took on the rating. When the rating no
    longer stands for that report, take the report back."""
    row = await update
    if row is None:
        await _take_back(rating)
        raise ReportClosed
    return row


async def _withdraw_report(row: Optional[Row], thread_id: str, turn_index: int) -> None:
    """Withdraw the report of a thumbs-down that is about to stop being one.
    Raises ``ReportRefused`` when the platform could not; the rating then
    stays, so a retry can finish."""
    if not (row and row["rating"] == "thumbs_down" and row.get("report_id")):
        return
    if not reports_enabled():
        # A report the team got before reports were turned off here cannot be
        # withdrawn now, and the rating is its only record.
        if row.get("reported_at"):
            raise ReportRefused(PlatformResult("unavailable"))
        return
    done = await withdraw_thumbs_down(ThumbsDown.from_row(row, thread_id, turn_index))
    if not done.ok:
        raise ReportRefused(done)


async def submit(
    thread_id: str,
    turn_index: int,
    user_id: str,
    *,
    rating: str,
    issue_categories: Optional[list[str]],
    comment: Optional[str],
    consent_human_review: bool,
    client: Optional[ReportClient],
) -> tuple[Row, Optional[str]]:
    """Rate a turn. A thumbs-down is also sent to the team as a report; a
    thumbs-up over one withdraws it. Returns the rating and what the platform
    said of its report, None when there was none to send."""
    prior = await get_feedback_for_turn(thread_id, turn_index, user_id)
    if rating != "thumbs_down":
        await _withdraw_report(prior, thread_id, turn_index)
    # Sent again over a thumbs-down the team already has, the rating keeps
    # its report, so a review asked for only now goes as its own refund
    # request, as one asked for from the sent dialog does, and is recorded
    # once the platform has it: recorded first, a refused one would read as
    # requested and never be offered again.
    review_later = bool(
        rating == "thumbs_down"
        and consent_human_review
        and _has_report(prior)
        and prior["reported_at"]
        and not prior["consent_human_review"]
    )
    row = await upsert_feedback(
        conversation_thread_id=thread_id,
        turn_index=turn_index,
        user_id=user_id,
        rating=rating,
        issue_categories=issue_categories,
        comment=comment,
        consent_human_review=consent_human_review and not review_later,
    )
    if not row:
        raise NotFound
    if not _has_report(row):
        return row, None
    thumbs = ThumbsDown.from_row(row, thread_id, turn_index)
    sent, row = await _send_report(row, thumbs, client)
    if not (sent.ok and review_later):
        return row, sent.status
    review = await request_thumbs_review(thumbs, client=client)
    if review.ok:
        row = await _kept(request_feedback_review(str(row["conversation_feedback_id"]), thumbs.report_id), thumbs)
    return row, review.status


async def follow_up(
    thread_id: str,
    turn_index: int,
    user_id: str,
    *,
    note: str,
    note_id: Optional[str],
    human_review: bool,
    client: Optional[ReportClient],
) -> Row:
    """Add to the report a thumbs-down sent: a human review asked for, a note
    posted under it, or with neither, the report sent again if the team never
    got it. Each step is posted once however often it is sent, so a retry
    after a 429 or 503 finishes the rest."""
    row = await get_feedback_for_turn(thread_id, turn_index, user_id)
    if not _has_report(row):
        raise NotFound
    thumbs = ThumbsDown.from_row(row, thread_id, turn_index)
    feedback_id = str(row["conversation_feedback_id"])

    if not row["reported_at"]:
        sent, row = await _send_report(row, thumbs, client)
        if not sent.ok:
            raise ReportRefused(sent)

    if human_review and not row["consent_human_review"]:
        review = await request_thumbs_review(thumbs, client=client)
        if not review.ok:
            raise ReportRefused(review)
        row = await _kept(request_feedback_review(feedback_id, thumbs.report_id), thumbs)

    if note:
        posted = await reply_to_thumbs_down(thumbs, note, note_id=note_id)
        if not posted.ok:
            raise ReportRefused(posted)
        row = await _kept(add_feedback_follow_up(feedback_id, thumbs.report_id, note_id, note), thumbs)
    return row


async def remove(thread_id: str, turn_index: int, user_id: str) -> str:
    """Remove a turn's rating, its report withdrawn first: the team's post
    loses the user's words and a pending human review is cancelled. Returns
    the removed rating's id."""
    row = await get_feedback_for_turn(thread_id, turn_index, user_id)
    if not row:
        raise NotFound
    await _withdraw_report(row, thread_id, turn_index)
    feedback_id = str(row["conversation_feedback_id"])
    await delete_feedback_by_id(feedback_id)
    return feedback_id


async def for_thread(thread_id: str, user_id: str) -> list[Row]:
    return await get_feedback_for_thread(thread_id, user_id)


async def sync_langsmith(conversation_feedback_id: str) -> None:
    """Write the rating as it now stands to LangSmith, or remove it there.
    Read again after the response, so a request that lost a race with another
    does not write its stale copy over the newer one."""
    if not tracing_enabled():
        return
    row = await get_feedback_by_id(conversation_feedback_id)
    if row is None:
        await asyncio.to_thread(remove_rating, conversation_feedback_id)
        return
    try:
        vault = await user_vault(str(row["user_id"]))
    except ScrubUnavailable:
        return
    await asyncio.to_thread(mirror_rating, row, vault)
