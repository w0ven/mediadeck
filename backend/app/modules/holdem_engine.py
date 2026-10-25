"""Deterministic, no-limit Texas hold'em (2–6 seats), with no I/O or randomness.

Cards are ``suit * 13 + rank - 2``; ascending seat numbers run clockwise.
The version-1 state is a private JSON document, NOT a client response: it contains
both the remaining deck and every hole card. Only ``view`` is safe to publish.

``bet`` is a seat's contribution on this street; ``committed`` is its entire
hand contribution, AFTER uncalled refunds. Before settlement, stacks plus
committed chips conserve the initial total. After settlement, stacks alone do.
``acted_bet`` records the price at the last action (None means not yet acted),
so cumulative short all-ins reopen raising only when a full raise is faced.
``pending`` is the clockwise list of seats still needing to act. No replay,
Python objects, integer dictionary keys, or external state are needed to resume.

Public functions never mutate their arguments. States are trusted engine output
(or its JSON round-trip), not arbitrary client-supplied game state.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations


class EngineError(ValueError):
    """An invalid game request; messages are suitable for Chinese interfaces."""


_NAMES = ("高牌", "一对", "两对", "三条", "顺子", "同花", "葫芦", "四条", "同花顺")
_RANK_LABELS = {11: "J", 12: "Q", 13: "K", 14: "A"}
_STREETS = ("preflop", "flop", "turn", "river")


def _integer(value, minimum: int, maximum: int | None = None) -> bool:
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def _validate_cards(cards: list[int], sizes: tuple[int, ...]) -> None:
    if (
        not isinstance(cards, list)
        or len(cards) not in sizes
        or any(not _integer(card, 0, 51) for card in cards)
        or len(set(cards)) != len(cards)
    ):
        raise EngineError("牌张数量、编码或唯一性不合法")


def _five_rank(cards) -> tuple:
    """Suit-independent rank, ordered from high card (0) to straight flush (8)."""
    ranks = sorted((card % 13 + 2 for card in cards), reverse=True)
    flush = all(card // 13 == cards[0] // 13 for card in cards[1:])
    straight = 0
    if ranks[0] == ranks[1] + 1 == ranks[2] + 2 == ranks[3] + 3 == ranks[4] + 4:
        straight = ranks[0]
    elif ranks == [14, 5, 4, 3, 2]:
        straight = 5
    if flush and straight:
        return (8, straight)
    if flush:
        return (5, *ranks)
    if straight:
        return (4, straight)
    counts = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    if len(counts) == 5:
        return (0, *ranks)
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    pattern = tuple(count for count, _ in groups)
    ordered = tuple(rank for _, rank in groups)
    if pattern == (4, 1):
        return (7, *ordered)
    if pattern == (3, 2):
        return (6, *ordered)
    if pattern == (3, 1, 1):
        return (3, *ordered)
    if pattern == (2, 2, 1):
        return (2, *ordered)
    return (1, *ordered)


def _hand_name(rank: tuple) -> str:
    return "皇家同花顺" if rank == (8, 14) else _NAMES[rank[0]]


def best_hand(cards: list[int]) -> tuple[tuple, str, list[int]]:
    """Return strength, Chinese name, and a canonical best five from 5–7 cards.

    Equal strengths NEVER compare suits. Among equivalent five-card subsets,
    the lexicographically smallest sorted card codes are chosen, making the
    returned cards independent of the input order. A royal flush ranks (8, 14).
    """
    _validate_cards(cards, (5, 6, 7))
    ordered = sorted(cards)
    if len(ordered) == 5:
        rank = _five_rank(ordered)
        return rank, _hand_name(rank), ordered
    best_rank = (-1,)
    best_cards = None
    for five in combinations(ordered, 5):
        rank = _five_rank(five)
        if rank > best_rank:
            best_rank, best_cards = rank, five
    return best_rank, _hand_name(best_rank), list(best_cards)


def hand_hint(hole: list[int], board: list[int]) -> str:
    """A made-hand hint; before the flop only pocket pairs count as a pair."""
    _validate_cards(hole, (2,))
    _validate_cards(board, (0, 3, 4, 5))
    if len(set(hole + board)) != len(hole + board):
        raise EngineError("底牌和公共牌不能重复")
    if board:
        rank, name, _ = best_hand(hole + board)
    else:
        ranks = sorted((card % 13 + 2 for card in hole), reverse=True)
        rank = (1, ranks[0]) if ranks[0] == ranks[1] else (0, *ranks)
        name = _hand_name(rank)
    labels = [_RANK_LABELS.get(value, str(value)) for value in rank[1:]]
    if rank == (8, 14):
        return name
    if rank[0] == 2:
        return f"{name} · {labels[0]} 和 {labels[1]}"
    if rank[0] == 6:
        return f"{name} · {labels[0]} 带 {labels[1]}"
    if rank[0] in (4, 5, 8):
        return f"{name} · {labels[0]} 高"
    return f"{name} · {labels[0]}"


def _clockwise(state: dict, after: int) -> list[dict]:
    return sorted(state["seats"], key=lambda player: (player["seat"] - after - 1) % 6)


def _seat(state: dict, number: int) -> dict:
    return next(player for player in state["seats"] if player["seat"] == number)


def _capable(player: dict) -> bool:
    return not player["folded"] and player["stack"] > 0


def _pay(player: dict, amount: int) -> None:
    player["stack"] -= amount
    player["bet"] += amount
    player["committed"] += amount


def _take(state: dict, count: int) -> list[int]:
    start = state["cursor"]
    state["cursor"] += count
    return state["deck"][start:start + count]


def new_hand(
    *, seats: list[dict], button: int, small_blind: int, big_blind: int, deck: list[int]
) -> dict:
    """Post blinds and deal two rounds, starting immediately left of the button.

    Short blinds post everything. With multiple players able to bet, the
    preflop bring-in remains the nominal big blind even if its poster is short.
    With only one player able to act, only an actual outstanding wager is due;
    that player cannot bet into an uncontested side pot.
    """
    if not isinstance(seats, list) or not 2 <= len(seats) <= 6:
        raise EngineError("每手需要 2 至 6 名玩家")
    numbers, names = set(), set()
    players = []
    for player in seats:
        if not isinstance(player, dict):
            raise EngineError("座位资料不合法")
        number, name, stack = player.get("seat"), player.get("player"), player.get("stack")
        if not _integer(number, 0, 5) or number in numbers:
            raise EngineError("座位号必须是互不重复的 0 至 5")
        if not isinstance(name, str) or not name or name in names:
            raise EngineError("玩家标识必须是互不重复的非空字符串")
        if not _integer(stack, 1):
            raise EngineError("开局筹码必须是正整数")
        numbers.add(number)
        names.add(name)
        players.append({
            "seat": number, "player": name, "initial_stack": stack, "stack": stack,
            "bet": 0, "committed": 0, "hole": [], "folded": False, "acted_bet": None,
        })
    if not _integer(button, 0, 5) or button not in numbers:
        raise EngineError("按钮必须位于本手参与者的座位")
    if not _integer(small_blind, 1) or not _integer(big_blind, small_blind):
        raise EngineError("盲注必须是正整数，且大盲不能小于小盲")
    _validate_cards(deck, (52,))
    state = {
        "version": 1, "button": button, "small_blind": small_blind, "big_blind": big_blind,
        "seats": sorted(players, key=lambda player: player["seat"]),
        "deck": list(deck), "cursor": 0, "burned": [], "board": [], "street": "preflop",
        "current_bet": big_blind, "min_raise": big_blind, "pending": [],
        "uncalled": {}, "finished": False, "result": None,
    }
    order = _clockwise(state, button)
    small = _seat(state, button) if len(players) == 2 else order[0]
    big = order[0] if len(players) == 2 else order[1]
    state["small_blind_seat"], state["big_blind_seat"] = small["seat"], big["seat"]
    _pay(small, min(small_blind, small["stack"]))
    _pay(big, min(big_blind, big["stack"]))
    for _ in range(2):
        for player in order:
            player["hole"].extend(_take(state, 1))
    state["pending"] = [
        player["seat"] for player in _clockwise(state, big["seat"]) if _capable(player)
    ]
    _progress(state)
    return state


def current_player(state: dict) -> str | None:
    if state["finished"] or not state["pending"]:
        return None
    return _seat(state, state["pending"][0])["player"]


def _raise_allowed(state: dict, player: dict) -> bool:
    if not any(other["seat"] != player["seat"] and _capable(other) for other in state["seats"]):
        return False
    last = player["acted_bet"]
    # A check does not surrender the right to raise an opening short all-in bet.
    return last is None or last == 0 or state["current_bet"] - last >= state["min_raise"]


def legal_actions(state: dict, player: str) -> dict | None:
    """Return current options, or None off-turn/after the hand.

    ``call`` is the amount actually payable, capped by the remaining stack.
    Raise amounts are STREET TOTALS, never increments. A short stack's sole
    permitted raise has min_raise_to == max_raise_to (an incomplete all-in).
    ``all_in == 0`` means it is illegal (raising not reopened, or no opponent
    can contest a further bet); otherwise it is the all-in street total.
    """
    if current_player(state) != player or state["finished"] or not state["pending"]:
        return None
    actor = _seat(state, state["pending"][0])
    due = max(0, state["current_bet"] - actor["bet"])
    total = actor["bet"] + actor["stack"]
    can_raise = total > state["current_bet"] and _raise_allowed(state, actor)
    return {
        "fold": True,
        "check": due == 0,
        "call": min(due, actor["stack"]),
        "min_raise_to": min(total, state["current_bet"] + state["min_raise"]) if can_raise else None,
        "max_raise_to": total if can_raise else None,
        "all_in": total if total <= state["current_bet"] or can_raise else 0,
    }


def apply_action(state: dict, player: str, action: str, amount: int | None = None) -> dict:
    options = legal_actions(state, player)
    if options is None:
        raise EngineError("当前不是该玩家的行动回合，或本手已经结束")
    if action not in ("fold", "check", "call", "bet", "raise", "all_in"):
        raise EngineError("不支持的德州动作")
    if action in ("bet", "raise"):
        if not _integer(amount, 1):
            raise EngineError("下注或加注到的总额必须是正整数")
        if (action == "bet") != (state["current_bet"] == 0):
            raise EngineError("无人下注时使用下注，已有下注时使用加注")
        if options["min_raise_to"] is None:
            raise EngineError("当前不能加注：加注权未重开或无人可以应注")
        if not options["min_raise_to"] <= amount <= options["max_raise_to"]:
            raise EngineError("加注总额不足最小加注额或超过剩余筹码")
    elif amount is not None:
        raise EngineError("只有下注和加注动作可以指定金额")
    if action == "check" and not options["check"]:
        raise EngineError("尚有下注需要跟进，不能过牌")
    if action == "call" and not options["call"]:
        raise EngineError("当前没有需要跟进的下注，请过牌")
    if action == "all_in" and not options["all_in"]:
        raise EngineError("当前不能全下：加注权未重开或无人可以应注")

    updated = deepcopy(state)
    actor = _seat(updated, updated["pending"].pop(0))
    if action == "fold":
        actor["folded"] = True
    else:
        target = actor["bet"]
        if action == "call":
            target += options["call"]
        elif action == "all_in":
            target = options["all_in"]
        elif action in ("bet", "raise"):
            target = amount
        _pay(actor, target - actor["bet"])
        if target > updated["current_bet"]:
            increment = target - updated["current_bet"]
            updated["min_raise"] = max(increment, updated["min_raise"])
            updated["current_bet"] = target
    actor["acted_bet"] = updated["current_bet"]
    pending = updated["pending"]
    updated["pending"] = [
        other["seat"] for other in _clockwise(updated, actor["seat"])
        if other["seat"] != actor["seat"] and _capable(other)
        and (other["seat"] in pending or other["bet"] < updated["current_bet"])
    ]
    _progress(updated)
    return updated


def _refund_uncalled(state: dict) -> None:
    ordered = sorted(state["seats"], key=lambda player: player["bet"], reverse=True)
    leader = ordered[0]
    refund = leader["bet"] - ordered[1]["bet"]
    if refund:
        leader["bet"] -= refund
        leader["committed"] -= refund
        leader["stack"] += refund
        name = leader["player"]
        state["uncalled"][name] = state["uncalled"].get(name, 0) + refund
    state["current_bet"] = max(player["bet"] for player in state["seats"])


def _pots(state: dict) -> list[dict]:
    pots = []
    previous = 0
    for level in sorted({player["committed"] for player in state["seats"] if player["committed"]}):
        contributors = [player for player in state["seats"] if player["committed"] >= level]
        amount = (level - previous) * len(contributors)
        eligible = [player["player"] for player in contributors if not player["folded"]]
        if pots and pots[-1]["eligible"] == eligible:
            pots[-1]["amount"] += amount
        else:
            pots.append({"amount": amount, "eligible": eligible, "winners": []})
        previous = level
    return pots


def _next_street(state: dict) -> None:
    street = _STREETS[_STREETS.index(state["street"]) + 1]
    state["street"] = street
    state["burned"].extend(_take(state, 1))
    state["board"].extend(_take(state, 3 if street == "flop" else 1))
    state["current_bet"] = 0
    state["min_raise"] = state["big_blind"]
    for player in state["seats"]:
        player["bet"] = 0
        player["acted_bet"] = None
    state["pending"] = [
        player["seat"] for player in _clockwise(state, state["button"]) if _capable(player)
    ]


def _progress(state: dict) -> None:
    """Advance only forced events; stop precisely at the next real decision."""
    while True:
        live = [player for player in state["seats"] if not player["folded"]]
        if len(live) == 1:
            _refund_uncalled(state)
            _finish(state, showdown=False)
            return
        capable = [player for player in live if _capable(player)]
        if len(capable) <= 1:
            # Do not skip a last player's outstanding call/fold decision. Nor
            # ask for a meaningless check or let them create a dry side pot.
            state["current_bet"] = max(player["bet"] for player in state["seats"])
            state["pending"] = [
                player["seat"] for player in capable if player["bet"] < state["current_bet"]
            ]
        if state["pending"]:
            return
        _refund_uncalled(state)
        if state["street"] == "river":
            _finish(state, showdown=True)
            return
        _next_street(state)


def _finish(state: dict, *, showdown: bool) -> None:
    pots = _pots(state)
    evaluated = {
        player["player"]: best_hand(player["hole"] + state["board"])
        for player in state["seats"] if showdown and not player["folded"]
    }
    won = {player["player"]: 0 for player in state["seats"]}
    payout_order = _clockwise(state, state["button"])
    for pot in pots:
        if showdown:
            strength = max(evaluated[name][0] for name in pot["eligible"])
            winners = {name for name in pot["eligible"] if evaluated[name][0] == strength}
        else:
            winners = set(pot["eligible"])
        ordered = [player for player in payout_order if player["player"] in winners]
        pot["winners"] = [player["player"] for player in ordered]
        share, odd = divmod(pot["amount"], len(ordered))
        for index, player in enumerate(ordered):
            payout = share + (index < odd)
            player["stack"] += payout
            won[player["player"]] += payout
    state["result"] = {
        "players": {
            player["player"]: {
                "stack": player["stack"], "net": player["stack"] - player["initial_stack"],
                "won": won[player["player"]],
                "hand": evaluated[player["player"]][1] if player["player"] in evaluated else None,
                "best5": evaluated[player["player"]][2] if player["player"] in evaluated else None,
                "shown": player["player"] in evaluated,
            }
            for player in state["seats"]
        },
        "pots": pots, "uncalled": dict(state["uncalled"]),
    }
    state["finished"] = True
    state["pending"] = []


def is_finished(state: dict) -> bool:
    return state["finished"]


def results(state: dict) -> dict:
    if not state["finished"]:
        raise EngineError("本手尚未结束，不能获取结算结果")
    return deepcopy(state["result"])


def view(state: dict, viewer: str | None) -> dict:
    """Detached public snapshot, with only the viewer's own or shown hole cards.

    Streets stay preflop/flop/turn/river even after completion; use ``finished``
    to distinguish a completed hand. Status is active/folded/all_in. ``pot``
    and ``pots`` show contributions while playing, awarded pots when finished.
    Folded/uncontested hands have no hand evaluation in the public result.
    """
    seats = []
    for player in state["seats"]:
        shown = state["finished"] and state["result"]["players"][player["player"]]["shown"]
        public = {
            "seat": player["seat"], "player": player["player"], "stack": player["stack"],
            "bet": player["bet"], "committed": player["committed"], "shown": shown,
            "status": "folded" if player["folded"] else "all_in" if not player["stack"] else "active",
        }
        if player["player"] == viewer or shown:
            public["hole"] = list(player["hole"])
        seats.append(public)
    pots = deepcopy(state["result"]["pots"]) if state["finished"] else _pots(state)
    return {
        "button": state["button"], "small_blind": state["small_blind"],
        "big_blind": state["big_blind"], "small_blind_seat": state["small_blind_seat"],
        "big_blind_seat": state["big_blind_seat"], "street": state["street"],
        "board": list(state["board"]), "seats": seats, "pots": pots,
        "pot": sum(pot["amount"] for pot in pots), "current_bet": state["current_bet"],
        "min_raise": state["min_raise"], "current_player": current_player(state),
        "legal_actions": legal_actions(state, viewer) if viewer is not None else None,
        "finished": state["finished"], "uncalled": dict(state["uncalled"]),
        "results": results(state) if state["finished"] else None,
    }


def auto_action(state: dict, player: str) -> tuple[str, int | None]:
    """Conservative made-hand policy; never actively bet, raise, or all-in."""
    options = legal_actions(state, player)
    if options is None:
        raise EngineError("当前不是该玩家的托管行动回合")
    if options["check"]:
        return "check", None
    actor = _seat(state, state["pending"][0])
    call = options["call"]
    pot = sum(other["committed"] for other in state["seats"])
    if 2 * call > pot:
        return "fold", None
    calls_all_in = call == actor["stack"]
    if not state["board"]:
        ranks = [card % 13 + 2 for card in actor["hole"]]
        made = ranks[0] == ranks[1] or min(ranks) >= 12
        follow = made and not calls_all_in
    else:
        rank, _, _ = best_hand(actor["hole"] + state["board"])
        follow = rank[0] >= (2 if calls_all_in else 1)
    return ("call", None) if follow else ("fold", None)
