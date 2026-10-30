"""Polls inside a meeting: the host asks, everyone votes, results update live."""
from collections import defaultdict

from sqlalchemy import delete, select

from backend.database import SessionLocal
from backend.models import Poll, PollOption, PollVote, User


class PollError(Exception):
    """A readable problem to show to the person who caused it."""


def poll_state(db, poll: Poll) -> dict:
    options = db.scalars(select(PollOption).where(PollOption.poll_id == poll.id).order_by(PollOption.position)).all()
    rows = db.execute(
        select(PollVote.option_id, PollVote.user_id, User.full_name)
        .join(User, User.id == PollVote.user_id).where(PollVote.poll_id == poll.id)
    ).all()
    names, voters = defaultdict(list), set()
    for option_id, user_id, name in rows:
        names[option_id].append(name)
        voters.add(user_id)
    return {
        "id": poll.id, "question": poll.question, "multiple": poll.multiple, "anonymous": poll.anonymous,
        "closed": poll.closed, "created_by": poll.creator_name, "total_voters": len(voters),
        "options": [
            {"id": o.id, "text": o.text, "count": len(names[o.id]), "names": [] if poll.anonymous else sorted(names[o.id])}
            for o in options
        ],
    }


def create_poll(meeting_id: int, user_id: int, user_name: str, question, options, multiple: bool, anonymous: bool) -> dict:
    question = str(question or "").strip()[:300]
    cleaned, seen = [], set()
    for raw in options if isinstance(options, list) else []:
        text = str(raw).strip()[:120]
        if text and text.lower() not in seen:
            cleaned.append(text)
            seen.add(text.lower())
    if not question or not 2 <= len(cleaned) <= 8:
        raise PollError("A poll needs a question and 2 to 8 different options.")
    with SessionLocal() as db:
        poll = Poll(meeting_id=meeting_id, creator_id=user_id, creator_name=user_name[:120], question=question,
                    multiple=multiple, anonymous=anonymous)
        db.add(poll)
        db.flush()
        for position, text in enumerate(cleaned):
            db.add(PollOption(poll_id=poll.id, text=text, position=position))
        db.commit()
        return poll_state(db, poll)


def cast_vote(meeting_id: int, user_id: int, poll_id, option_ids) -> tuple[dict, list[int]]:
    """Replace this person's vote. Returns (poll state, the options they now have chosen)."""
    try:
        poll_id = int(poll_id)
        wanted = {int(x) for x in (option_ids if isinstance(option_ids, list) else [])}
    except (TypeError, ValueError):
        raise PollError("That vote could not be read.") from None
    with SessionLocal() as db:
        poll = db.get(Poll, poll_id)
        if poll is None or poll.meeting_id != meeting_id:
            raise PollError("That poll does not exist.")
        if poll.closed:
            raise PollError("This poll is closed.")
        valid = set(db.scalars(select(PollOption.id).where(PollOption.poll_id == poll.id)).all())
        chosen = wanted & valid
        if not poll.multiple and len(chosen) > 1:
            raise PollError("Choose only one option.")
        db.execute(delete(PollVote).where(PollVote.poll_id == poll.id, PollVote.user_id == user_id))
        for option_id in chosen:
            db.add(PollVote(poll_id=poll.id, option_id=option_id, user_id=user_id))
        db.commit()
        return poll_state(db, poll), sorted(chosen)


def close_poll(meeting_id: int, poll_id) -> dict:
    try:
        poll_id = int(poll_id)
    except (TypeError, ValueError):
        raise PollError("That poll does not exist.") from None
    with SessionLocal() as db:
        poll = db.get(Poll, poll_id)
        if poll is None or poll.meeting_id != meeting_id:
            raise PollError("That poll does not exist.")
        poll.closed = True
        db.commit()
        return poll_state(db, poll)


def list_for_meeting(meeting_id: int, user_id: int) -> tuple[list[dict], dict[int, list[int]]]:
    """Every poll of the meeting (for people who join later) and this person's own choices."""
    with SessionLocal() as db:
        polls = db.scalars(select(Poll).where(Poll.meeting_id == meeting_id).order_by(Poll.id)).all()
        states = [poll_state(db, p) for p in polls]
        mine: dict[int, list[int]] = defaultdict(list)
        if polls:
            for poll_id, option_id in db.execute(
                select(PollVote.poll_id, PollVote.option_id).where(
                    PollVote.user_id == user_id, PollVote.poll_id.in_([p.id for p in polls]))
            ).all():
                mine[poll_id].append(option_id)
        return states, dict(mine)
