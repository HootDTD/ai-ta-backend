"""P3.5 — Apollo OLM clarification-invite logic.

When the parser writes new KG nodes with low `parser_confidence`, we want
Apollo to invite the student to clarify — without putting words in their
mouth, without naming the wobble.

Trigger contract (per design):
    - "Low-confidence pattern" = the parser produced one or more new nodes
      this turn with `parser_confidence < LOW_CONF_THRESHOLD` (0.7).
    - The counter is the count of past student turns that had this pattern
      plus (1 if this turn does). Invite fires when counter >= 2 and the
      cooldown has expired.
    - Cooldown defaults to 60 seconds since the last fired invite.
    - Master env flag `APOLLO_OLM_INVITES_ENABLED` (default off). When off,
      the analytics flag still rides on student message metadata so we
      can calibrate the threshold against real sessions before flipping
      the flag globally.

The invite asks Apollo to "ask one ignorant clarifying question whose
answer would let you confirm or fix" the lowest-confidence new entry.
The entry id is surfaced on the chat envelope so the FE can highlight
which pill the student should look at — closing the loop between
backend trigger and UI affordance.

Research anchor:
    Mr Collins (Bull & Pain 1995) shows that initiative-mixing — the
    system asking the student rather than always vice-versa — drives
    the metacognitive gain. The invite makes the system the one to
    say "I might have misheard you." In the dual-belief frame, that's
    the system flagging its own uncertainty.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.ontology import Node
from apollo.persistence.models import TutoringMessage


LOW_CONF_THRESHOLD: float = 0.7
COUNTER_THRESHOLD: int = 2
COOLDOWN_SECONDS: int = 60
ENV_FLAG: str = "APOLLO_OLM_INVITES_ENABLED"


def is_enabled() -> bool:
    """Master gate. Controls whether the persona-shift invite text is
    actually appended to Apollo's prompt. Analytics tracking runs either
    way."""
    return os.environ.get(ENV_FLAG, "").lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class OlmInviteSignal:
    """Per-turn decision the chat handler propagates downstream.

    `fired` is the master boolean — when False, the suffix is empty and
    the FE envelope shows {fired: False}. `entry_id` and `summary` are
    only meaningful when fired=True; they pin which pill the FE should
    pulse. `low_conf_pattern_this_turn` is always set so it can be
    persisted on the student's message metadata for the next turn's
    counter — it's analytics, not gating.
    """
    fired: bool
    entry_id: str | None = None
    summary: str | None = None
    low_conf_pattern_this_turn: bool = False


def find_low_conf_new_nodes(
    nodes: Iterable[Node],
    *,
    threshold: float = LOW_CONF_THRESHOLD,
) -> list[Node]:
    """Filter the just-parsed nodes for those with parser_confidence below
    the threshold. Pure function — no state."""
    return [n for n in nodes if n.parser_confidence < threshold]


def _node_summary(node: Node) -> str:
    """Short human-readable summary of a node for the FE's invite-pulse
    UI. Different node types have different surface forms — pick the
    most readable bit per type. Capped to a one-liner."""
    c = node.content.model_dump()
    if node.node_type == "equation":
        return c.get("symbolic", "")[:120]
    if node.node_type == "condition":
        return (c.get("applies_when") or c.get("label") or "")[:120]
    if node.node_type == "simplification":
        return (c.get("transformation") or "")[:120]
    if node.node_type == "definition":
        return f"{c.get('concept', '')} = {c.get('meaning', '')}"[:120]
    if node.node_type == "variable_mapping":
        return f"{c.get('term', '')} → {c.get('symbol', '')}"[:120]
    if node.node_type == "procedure_step":
        return (c.get("action") or "")[:120]
    return ""


async def _count_past_low_conf_patterns(
    db: AsyncSession, *, session_id: int,
) -> int:
    """Count past STUDENT turns flagged as low-conf pattern. Reads
    message_metadata of role=student rows."""
    rows = (await db.execute(
        select(TutoringMessage.message_metadata)
        .where(TutoringMessage.session_id == session_id)
        .where(TutoringMessage.role == "student")
    )).scalars().all()
    n = 0
    for payload in rows:
        if isinstance(payload, dict) and payload.get("low_conf_pattern"):
            n += 1
    return n


async def _last_invite_at(
    db: AsyncSession, *, session_id: int,
) -> datetime | None:
    """Most-recent created_at among Apollo turns where olm_invite.fired
    was True. Returns None if no invite has fired yet in this session."""
    rows = (await db.execute(
        select(TutoringMessage.message_metadata, TutoringMessage.created_at)
        .where(TutoringMessage.session_id == session_id)
        .where(TutoringMessage.role == "apollo")
        .order_by(TutoringMessage.turn_index.desc())
    )).all()
    for payload, created_at in rows:
        if not isinstance(payload, dict):
            continue
        inv = payload.get("olm_invite")
        if isinstance(inv, dict) and inv.get("fired"):
            return created_at
    return None


def _cooldown_expired(
    last_invite: datetime | None, now: datetime, *, seconds: int = COOLDOWN_SECONDS,
) -> bool:
    """True iff there's been no recent fired invite within the cooldown window.

    Defensive against tz-naive timestamps coming back from SQLite (which
    drops the tz on TIMESTAMP(timezone=True) columns). Postgres always
    returns tz-aware values; SQLite-in-tests does not. We assume UTC for
    both sides — the column is `now()` on Postgres which is UTC-anchored
    in our infra, and tests build their datetimes in UTC.
    """
    if last_invite is None:
        return True
    li = last_invite if last_invite.tzinfo else last_invite.replace(tzinfo=timezone.utc)
    nw = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    delta = (nw - li).total_seconds()
    return delta >= seconds


async def decide_invite(
    *,
    db: AsyncSession,
    session_id: int,
    new_low_conf_nodes: list[Node],
    now: datetime | None = None,
) -> OlmInviteSignal:
    """Compute the per-turn invite decision.

    Always tracks `low_conf_pattern_this_turn` in the returned signal —
    callers persist that flag on the student's message metadata for the
    next turn's counter, regardless of master flag state.

    The actual fire (entry_id + summary populated, fired=True) requires:
        - master flag on,
        - low-conf pattern this turn,
        - counter (past low-conf patterns + 1 for this turn) >= threshold,
        - cooldown elapsed since last fired invite.
    """
    pattern_this_turn = bool(new_low_conf_nodes)
    base = OlmInviteSignal(
        fired=False,
        low_conf_pattern_this_turn=pattern_this_turn,
    )

    if not is_enabled() or not pattern_this_turn:
        return base

    past = await _count_past_low_conf_patterns(db, session_id=session_id)
    counter = past + 1
    if counter < COUNTER_THRESHOLD:
        return base

    last = await _last_invite_at(db, session_id=session_id)
    now_ts = now or datetime.now(timezone.utc)
    if not _cooldown_expired(last, now_ts):
        return base

    # Pick the single lowest-confidence node — that's the entry the FE
    # will highlight and Apollo's invite will (semantically) target.
    lowest = min(new_low_conf_nodes, key=lambda n: n.parser_confidence)
    return OlmInviteSignal(
        fired=True,
        entry_id=lowest.node_id,
        summary=_node_summary(lowest),
        low_conf_pattern_this_turn=True,
    )


def signal_to_metadata(signal: OlmInviteSignal) -> dict:
    """Serialize a signal for persistence on TutoringMessage.metadata. The summary
    is omitted from the audit trail (it's redundant with the node itself,
    which Neo4j already stores) — only the gate-relevant fields stick."""
    return {
        "fired": signal.fired,
        "entry_id": signal.entry_id,
    }
