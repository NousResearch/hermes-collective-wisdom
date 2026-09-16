"""Local, editable publisher evidence carried by the approved author description."""
# pylint: disable=too-many-arguments

from datetime import datetime, timedelta, timezone, tzinfo
from .qualification import _host_timezone
from .store import WisdomStore


def publication_description(
    store: WisdomStore,
    skill_id: str,
    description: str,
    *,
    at: datetime | None = None,
    timezone_info: tuple[tzinfo, str] | None = None,
    timezone_provider=None,
) -> str:
    """Snapshot one week of local use; never send a ledger or claim verified success.

    This publication snapshot is independent of recommendation thresholds.
    Call only while preparing a new local review, never on upload or retry.
    """
    profile_tz, timezone_name = timezone_info or (timezone_provider or _host_timezone)()
    current = at or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("publisher evidence timestamps must include a timezone")
    end = current.astimezone(profile_tz).date()
    start = end - timedelta(days=6)
    with store.transaction() as db:
        count, days = db.execute(
            "SELECT COALESCE(SUM(u.use_count),0),COUNT(*) "
            "FROM usage_day u JOIN local_skill s ON s.id=u.skill_id "
            "WHERE s.id=? AND s.source_kind='local' AND s.deleted_at IS NULL "
            "AND u.timezone_name=? AND u.day_local>=? AND u.day_local<=? "
            "AND u.use_count>0",
            (skill_id, timezone_name, start.isoformat(), end.isoformat()),
        ).fetchone()
    if not count:
        return description
    return (
        f"{description}\n\nPublisher usage (client-reported)\n"
        f"Local ledger: {count} invocation{'s' if count != 1 else ''} "
        f"on {days} day{'s' if days != 1 else ''}, "
        f"{start.isoformat()} through {end.isoformat()} (profile calendar dates). "
        "Counts may span local revisions and do not verify successful outcomes."
    )


def evidence_block(description: str) -> str | None:
    """Return the exact frozen evidence suffix already present in reviewed copy."""
    marker = "\n\nPublisher usage (client-reported)\n"
    if marker not in description:
        return None
    return "Publisher usage (client-reported)\n" + description.split(marker, 1)[1]
