"""Rule examples plus complete five-card enumeration and 10,000 seeded hands.

The two exhaustive/property tests intentionally run by default; no plugin,
network, cached result, or optional dependency is needed.
"""
from __future__ import annotations

import json
import random
from collections import Counter
from copy import deepcopy
from itertools import combinations

import pytest

from app.modules.holdem_engine import (
    EngineError,
    apply_action,
    auto_action,
    best_hand,
    current_player,
    hand_hint,
    is_finished,
    legal_actions,
    new_hand,
    results,
    view,
)


def cards(text):
    return ["shcd".index(card[1]) * 13 + "23456789TJQKA".index(card[0]) for card in text.split()]


def seats(stacks, positions=None):
    if positions is None:
        positions = range(len(stacks))
    return [{"seat": number, "player": f"p{number}", "stack": stack}
            for number, stack in zip(positions, stacks, strict=True)]


def start(stacks=(200, 200, 200), *, button=0, small=5, big=10, deck=None, positions=None):
    return new_hand(seats=seats(stacks, positions), button=button, small_blind=small,
                    big_blind=big, deck=list(range(52)) if deck is None else deck)


def act(state, action, amount=None, *, player=None):
    if player is not None:
        assert current_player(state) == player
    return apply_action(state, current_player(state), action, amount)


def check_down(state):
    for _ in range(30):
        if is_finished(state):
            return state
        options = legal_actions(state, current_player(state))
        state = act(state, "check" if options["check"] else "call")
    pytest.fail("无下注时未能在有限步内结束")


def rigged_deck(holes, board, button=0):
    """Build a genuine shuffled permutation with the prescribed deal and burns."""
    reserved = [card for hole in holes.values() for card in hole] + board
    assert len(reserved) == len(set(reserved))
    remaining = sorted(set(range(52)) - set(reserved))
    order = sorted(holes, key=lambda number: (number - button - 1) % 6)
    prefix = [holes[number][round_] for round_ in range(2) for number in order]
    for street in (board[:3], board[3:4], board[4:]):
        prefix.append(remaining.pop(0))
        prefix.extend(street)
    return prefix + remaining


def rigged(holes, board, stacks=None, *, button=0, small=5, big=10):
    return start(stacks or [200] * len(holes), button=button, small=small, big=big,
                 deck=rigged_deck({number: cards(hole) for number, hole in enumerate(holes)},
                                  cards(board), button))


def assert_integrity(state, total):
    restored = json.loads(json.dumps(state))
    assert restored == state
    assert current_player(restored) == current_player(state)
    assert is_finished(restored) == is_finished(state)
    assert sum(player["initial_stack"] for player in state["seats"]) == total
    assert state["cursor"] <= 52
    assert state["min_raise"] >= state["big_blind"]
    assert state["current_bet"] >= 0
    dealt = state["board"] + state["burned"] + [
        card for player in state["seats"] for card in player["hole"]
    ]
    assert len(dealt) == len(set(dealt)) == state["cursor"]
    assert set(dealt) == set(state["deck"][:state["cursor"]])
    assert len(state["board"]) in (0, 3, 4, 5)
    for player in state["seats"]:
        assert 0 <= player["bet"] <= player["committed"] <= player["initial_stack"]
        assert player["stack"] >= 0
        if not is_finished(state):
            assert player["stack"] + player["committed"] == player["initial_stack"]
    assert all(amount > 0 for amount in state["uncalled"].values())
    stacks = sum(player["stack"] for player in state["seats"])
    if is_finished(state):
        assert stacks == total
        outcome = results(state)
        assert results(restored) == outcome
        assert sum(player["net"] for player in outcome["players"].values()) == 0
        assert sum(player["won"] for player in outcome["players"].values()) == sum(
            pot["amount"] for pot in outcome["pots"]
        ) == sum(player["committed"] for player in state["seats"])
        for player in state["seats"]:
            result = outcome["players"][player["player"]]
            assert result["stack"] == player["stack"]
            assert result["net"] == result["stack"] - player["initial_stack"]
            assert result["stack"] == player["initial_stack"] - player["committed"] + result["won"]
            assert result["won"] >= 0
        for pot in outcome["pots"]:
            assert pot["amount"] > 0
            assert pot["winners"] and set(pot["winners"]) <= set(pot["eligible"])
    else:
        assert stacks + sum(player["committed"] for player in state["seats"]) == total
        player = current_player(state)
        assert player is not None
        assert legal_actions(restored, player) == legal_actions(state, player)
        assert view(restored, player) == view(state, player)
    return restored


def test_exhaustive_all_2598960_five_card_hands():
    counts = Counter()
    for combination in combinations(range(52), 5):
        strength, name, five = best_hand(list(combination))
        counts[name] += 1
        assert five == list(combination)
        assert 0 <= strength[0] <= 8
    assert counts == {
        "皇家同花顺": 4, "同花顺": 36, "四条": 624, "葫芦": 3744,
        "同花": 5108, "顺子": 10200, "三条": 54912, "两对": 123552,
        "一对": 1098240, "高牌": 1302540,
    }
    assert sum(counts.values()) == 2598960
    assert counts["同花顺"] + counts["皇家同花顺"] == 40


@pytest.mark.parametrize(("text", "expected", "name", "five"), [
    ("As 2h 3c 4d 5s Kh Qc", (4, 5), "顺子", "As 2h 3c 4d 5s"),
    ("As Js 9s 5s 2s 3h 4d", (5, 14, 11, 9, 5, 2), "同花", "As Js 9s 5s 2s"),
    ("Ks Kh Kc 7s 7h 7c 2d", (6, 13, 7), "葫芦", "Ks Kh Kc 7s 7h"),
    ("As Ah Ks Kh Qs Qh Jc", (2, 14, 13, 12), "两对", "As Ah Ks Kh Qs"),
    ("As 2s 3s 4s 5s Kh Qc", (8, 5), "同花顺", "As 2s 3s 4s 5s"),
    ("Ts Js Qs Ks As 2h 3c", (8, 14), "皇家同花顺", "Ts Js Qs Ks As"),
    ("Ks Kh Kc Kd As Ah 2c", (7, 13, 14), "四条", "Ks Kh Kc Kd As"),
    ("As Ah Ac Ks Kh Qs", (6, 14, 13), "葫芦", "As Ah Ac Ks Kh"),
])
def test_best_five_of_six_or_seven(text, expected, name, five):
    hand = cards(text)
    before = list(hand)
    result = best_hand(hand)
    assert result == (expected, name, sorted(cards(five)))
    assert best_hand(list(reversed(hand))) == result
    assert best_hand(result[2])[0] == expected
    assert hand == before


@pytest.mark.parametrize(("higher", "lower"), [
    ("As Ah Ac Ad Ks", "As Ah Ac Ad Qs"),
    ("As Ah Ac Ks Kh", "As Ah Ac Qs Qh"),
    ("As Js 8s 5s 3s", "Ah Jh 8h 5h 2h"),
    ("2s 3h 4c 5d 6s", "As 2h 3c 4d 5s"),
    ("As Ah Ac Ks Qh", "As Ah Ac Ks Jh"),
    ("As Ah Ks Kh Qs", "As Ah Ks Kh Js"),
    ("As Ah Ks Qh 9s", "As Ah Ks Qh 8s"),
    ("As Kh Qc 8d 4s", "As Kh Qc 8d 3s"),
])
def test_kickers_compare_to_the_last_card(higher, lower):
    assert best_hand(cards(higher))[0] > best_hand(cards(lower))[0]


def test_suits_do_not_break_a_tie():
    assert best_hand(cards("As Js 9s 5s 2s"))[0] == best_hand(cards("Ah Jh 9h 5h 2h"))[0]


@pytest.mark.parametrize(("hole", "board", "hint"), [
    ("As Ah", "", "一对 · A"),
    ("Ks Qh", "", "高牌 · K"),
    ("Ks Kh", "7s 7h 2c", "两对 · K 和 7"),
    ("As Kh", "Ac 7s 2h", "一对 · A"),
    ("Ks Qh", "Js 8h 2c", "高牌 · K"),
    ("As 2h", "3c 4d 5s", "顺子 · 5 高"),
    ("Ks Kh", "Kc 7s 7h", "葫芦 · K 带 7"),
    ("Ts Js", "Qs Ks As", "皇家同花顺"),
])
def test_hand_hints(hole, board, hint):
    assert hand_hint(cards(hole), cards(board)) == hint


def test_heads_up_sparse_seats_dealing_and_all_street_order():
    state = start([100, 100], positions=[0, 4], button=4, small=1, big=2)
    assert state["small_blind_seat"] == 4
    assert state["big_blind_seat"] == 0
    assert [(p["hole"], p["bet"]) for p in state["seats"]] == [([0, 2], 2), ([1, 3], 1)]
    assert current_player(state) == "p4"
    state = act(state, "call", player="p4")
    state = act(state, "check", player="p0")
    assert state["board"] == [5, 6, 7]
    for expected_street in ("flop", "turn", "river"):
        assert state["street"] == expected_street
        state = act(state, "check", player="p0")
        state = act(state, "check", player="p4")
    assert is_finished(state)
    assert state["board"] == [5, 6, 7, 9, 11]
    assert state["burned"] == [4, 8, 10]
    assert state["cursor"] == 12
    assert_integrity(state, 200)


@pytest.mark.parametrize(("positions", "button", "sb", "bb", "pre", "post"), [
    ([5, 1, 3], 3, 5, 1, [3, 5, 1], [5, 1, 3]),
    ([0, 1, 2, 3, 4, 5], 3, 4, 5, [0, 1, 2, 3, 4, 5], [4, 5, 0, 1, 2, 3]),
])
def test_three_and_six_player_positions(positions, button, sb, bb, pre, post):
    state = start([100] * len(positions), positions=positions, button=button)
    assert (state["small_blind_seat"], state["big_blind_seat"]) == (sb, bb)
    deal_order = sorted(positions, key=lambda number: (number - button - 1) % 6)
    for player in state["seats"]:
        index = deal_order.index(player["seat"])
        assert player["hole"] == [index, index + len(positions)]
    for number in pre:
        options = legal_actions(state, f"p{number}")
        assert options is not None
        state = act(state, "check" if options["check"] else "call", player=f"p{number}")
    assert state["street"] == "flop"
    for number in post:
        state = act(state, "check", player=f"p{number}")
    assert state["street"] == "turn"


def test_postflop_skips_folded_and_all_in_seats():
    state = start([200, 7, 200, 200], button=0)
    state = act(state, "call", player="p3")
    state = act(state, "fold", player="p0")
    state = act(state, "call", player="p1")
    state = act(state, "check", player="p2")
    assert state["street"] == "flop"
    assert current_player(state) == "p2"
    assert state["pending"] == [2, 3]


def test_minimum_raise_is_last_full_increment_and_resets_each_street():
    state = start([500, 500, 500])
    assert legal_actions(state, "p0")["min_raise_to"] == 20
    state = act(state, "raise", 30)
    assert legal_actions(state, "p1")["min_raise_to"] == 50
    with pytest.raises(EngineError, match="最小加注"):
        act(state, "raise", 49)
    state = act(state, "raise", 50)
    assert legal_actions(state, "p2")["min_raise_to"] == 70
    state = act(state, "call")
    state = act(state, "call")
    assert current_player(state) == "p1"
    assert legal_actions(state, "p1")["min_raise_to"] == 10
    with pytest.raises(EngineError):
        act(state, "bet", 9)
    state = act(state, "bet", 15)
    assert legal_actions(state, "p2")["min_raise_to"] == 30


def short_raise_state():
    state = act(start([200, 35, 200]), "raise", 30)
    assert legal_actions(state, "p1")["min_raise_to"] == 35
    assert legal_actions(state, "p1")["max_raise_to"] == 35
    return act(state, "all_in", player="p1")


def test_incomplete_all_in_does_not_reopen_prior_actor():
    state = act(short_raise_state(), "call", player="p2")
    assert legal_actions(state, "p0") == {
        "fold": True, "check": False, "call": 5, "min_raise_to": None,
        "max_raise_to": None, "all_in": 0,
    }
    before = deepcopy(state)
    for action, amount in (("raise", 55), ("all_in", None)):
        with pytest.raises(EngineError, match="加注权未重开"):
            act(state, action, amount)
        assert state == before
    state = act(state, "call")
    assert state["street"] == "flop"
    assert current_player(state) == "p2"


def test_not_yet_acted_player_can_raise_after_short_all_in():
    state = short_raise_state()
    assert legal_actions(state, "p2")["min_raise_to"] == 55
    state = act(state, "raise", 55)
    assert legal_actions(state, "p0")["min_raise_to"] == 75
    assert legal_actions(state, "p0")["call"] == 25


def test_cumulative_short_all_ins_reopen_only_after_a_full_increment_is_faced():
    state = start([35, 50, 200, 200])
    state = act(state, "raise", 30, player="p3")
    state = act(state, "all_in", player="p0")
    state = act(state, "all_in", player="p1")
    assert state["min_raise"] == 20
    state = act(state, "call", player="p2")
    assert legal_actions(state, "p3")["min_raise_to"] == 70
    state = act(state, "raise", 70)
    assert legal_actions(state, "p2")["min_raise_to"] == 90

    state = start([200, 50, 200, 200, 35])
    for player, action, amount in [
        ("p3", "raise", 30), ("p4", "all_in", None), ("p0", "call", None),
        ("p1", "all_in", None), ("p2", "call", None), ("p3", "call", None),
    ]:
        state = act(state, action, amount, player=player)
    assert legal_actions(state, "p0")["call"] == 15
    assert legal_actions(state, "p0")["min_raise_to"] is None


def test_check_retains_raise_right_against_short_opening_bet():
    state = start([200, 200, 15])
    for action in ("call", "call", "check", "check", "all_in", "call"):
        state = act(state, action)
    assert state["street"] == "flop"
    assert current_player(state) == "p1"
    assert state["current_bet"] == 5
    assert legal_actions(state, "p1")["min_raise_to"] == 15
    state = act(state, "raise", 15)
    assert legal_actions(state, "p0")["call"] == 10


def test_call_can_be_partial_all_in_without_being_a_raise():
    state = start([7, 100, 100])
    options = legal_actions(state, "p0")
    assert options["call"] == options["all_in"] == 7
    assert options["min_raise_to"] is None
    assert act(state, "call") == act(state, "all_in")
    state = act(state, "call")
    assert state["seats"][0]["stack"] == 0
    assert state["seats"][0]["bet"] == 7
    state = act(state, "call")
    state = act(state, "check")
    assert not is_finished(state)
    assert state["street"] == "flop"  # Two other players can still bet.
    assert current_player(state) == "p1"
    assert_integrity(check_down(state), 207)


@pytest.mark.parametrize(("stacks", "uncalled"), [
    ([100, 3], {"p0": 2}), ([3, 100], {"p1": 7}), ([3, 3], {}),
    ([1, 2], {"p1": 1}),
])
def test_short_blinds_automatic_runout(stacks, uncalled):
    state = start(stacks)
    assert is_finished(state)
    assert len(state["board"]) == 5
    assert results(state)["uncalled"] == uncalled
    assert all(player["shown"] for player in results(state)["players"].values())
    assert_integrity(state, sum(stacks))


def test_short_big_blind_nominal_bring_in_and_last_player_actual_call():
    state = start([100, 100, 3])
    assert legal_actions(state, "p0")["call"] == 10
    assert legal_actions(state, "p0")["min_raise_to"] == 20
    state = act(state, "call")
    state = act(state, "call")
    assert state["street"] == "flop"
    assert [player["committed"] for player in state["seats"]] == [10, 10, 3]

    state = start([100, 7])
    assert not is_finished(state)
    assert legal_actions(state, "p0")["call"] == 2
    assert legal_actions(state, "p0")["min_raise_to"] is None
    assert legal_actions(state, "p0")["all_in"] == 0
    with pytest.raises(EngineError):
        act(state, "all_in")
    assert_integrity(act(state, "call"), 107)
    assert is_finished(act(state, "call"))


def test_last_capable_player_must_still_call_or_fold():
    state = act(start([100, 50]), "all_in", player="p0")
    assert not is_finished(state)
    assert current_player(state) == "p1"
    assert legal_actions(state, "p1")["call"] == 40
    called = act(state, "call")
    assert is_finished(called) and len(called["board"]) == 5
    assert results(called)["uncalled"] == {"p0": 50}
    folded = act(state, "fold")
    assert is_finished(folded) and folded["board"] == []
    assert results(folded)["uncalled"] == {"p0": 90}
    assert all(not player["shown"] for player in results(folded)["players"].values())


def test_main_and_multiple_side_pots_with_uncalled_refund():
    state = rigged(["As Ah", "Ks Kh", "Qs Qh", "Js Jh"], "2s 3h 7d 8c 9s",
                   [50, 100, 200, 300])
    state = act(state, "all_in", player="p3")
    for player in ("p0", "p1", "p2"):
        state = act(state, "call", player=player)
    assert is_finished(state)
    outcome = results(state)
    assert outcome["uncalled"] == {"p3": 100}
    assert outcome["pots"] == [
        {"amount": 200, "eligible": ["p0", "p1", "p2", "p3"], "winners": ["p0"]},
        {"amount": 150, "eligible": ["p1", "p2", "p3"], "winners": ["p1"]},
        {"amount": 200, "eligible": ["p2", "p3"], "winners": ["p2"]},
    ]
    assert [p["stack"] for p in outcome["players"].values()] == [200, 150, 200, 100]
    assert [p["won"] for p in outcome["players"].values()] == [200, 150, 200, 0]
    assert [p["net"] for p in outcome["players"].values()] == [150, 50, 0, -200]
    assert all(p["shown"] and len(p["best5"]) == 5 for p in outcome["players"].values())
    assert_integrity(state, 650)


def test_folded_dead_money_is_in_pots_but_not_eligible():
    state = rigged(["As Ah", "Js Jh", "Ks Kh", "Qs Qh"], "2s 3h 7d 8c 9s",
                   [100, 100, 30, 100])
    for player, action, amount in [
        ("p3", "raise", 20), ("p0", "call", None), ("p1", "call", None),
        ("p2", "all_in", None), ("p3", "raise", 50), ("p0", "call", None),
        ("p1", "fold", None),
    ]:
        state = act(state, action, amount, player=player)
    state = check_down(state)
    outcome = results(state)
    assert outcome["pots"] == [
        {"amount": 110, "eligible": ["p0", "p2", "p3"], "winners": ["p0"]},
        {"amount": 40, "eligible": ["p0", "p3"], "winners": ["p0"]},
    ]
    assert outcome["players"]["p1"] == {
        "stack": 80, "net": -20, "won": 0, "hand": None, "best5": None, "shown": False,
    }
    assert_integrity(state, 330)


def test_board_plays_and_odd_chip_goes_to_first_winner_left_of_button():
    state = rigged(["2h 3h", "4h 5h", "6h 7h"], "Ts Js Qs Ks As",
                   [20, 20, 20], small=1, big=2)
    state = act(state, "raise", 4)
    state = act(state, "call")
    state = act(state, "call")
    assert state["street"] == "flop"
    all_show = check_down(state)
    assert [p["stack"] for p in results(all_show)["players"].values()] == [20, 20, 20]
    assert all(p["best5"] == sorted(cards("Ts Js Qs Ks As"))
               for p in results(all_show)["players"].values())

    state = rigged(["2h 3h", "4h 5h", "6h 7h"], "Ts Js Qs Ks As",
                   [20, 20, 20], small=1, big=2)
    state = act(state, "raise", 5)
    state = act(state, "call")
    state = act(state, "call")
    state = act(state, "fold", player="p1")
    state = check_down(state)
    outcome = results(state)
    assert outcome["pots"] == [{"amount": 15, "eligible": ["p0", "p2"], "winners": ["p2", "p0"]}]
    assert outcome["players"]["p2"]["won"] == 8
    assert outcome["players"]["p0"]["won"] == 7
    assert not outcome["players"]["p1"]["shown"]
    assert_integrity(state, 60)


def test_fold_ends_immediately_refunds_excess_and_never_shows_winner():
    state = act(start([100, 100]), "raise", 50)
    state = act(state, "fold")
    outcome = results(state)
    assert state["board"] == []
    assert outcome["uncalled"] == {"p0": 40}
    assert outcome["pots"] == [{"amount": 20, "eligible": ["p0"], "winners": ["p0"]}]
    assert outcome["players"]["p0"] == {
        "stack": 110, "net": 10, "won": 20, "hand": None, "best5": None, "shown": False,
    }
    for viewer in (None, "p1", "unknown"):
        assert "hole" not in view(state, viewer)["seats"][0]
    assert view(state, "p0")["seats"][0]["hole"] == state["seats"][0]["hole"]
    assert_integrity(state, 200)


def assert_private_view(state):
    for viewer in (None, "unknown", *(player["player"] for player in state["seats"])):
        snapshot = view(state, viewer)
        assert not {"deck", "cursor", "burned", "pending", "result"} & snapshot.keys()
        assert json.loads(json.dumps(snapshot)) == snapshot
        for private, public in zip(state["seats"], snapshot["seats"], strict=True):
            shown = is_finished(state) and results(state)["players"][private["player"]]["shown"]
            assert public["shown"] == shown
            if private["player"] == viewer or shown:
                assert public["hole"] == private["hole"]
            else:
                assert "hole" not in public
            assert "acted_bet" not in public
            assert public["status"] in ("active", "folded", "all_in")


def test_views_hide_holes_until_showdown_and_do_not_share_mutable_objects():
    state = start()
    assert_private_view(state)
    state = act(state, "fold", player="p0")
    while not is_finished(state):
        assert_private_view(state)
        opts = legal_actions(state, current_player(state))
        state = act(state, "check" if opts["check"] else "call")
    assert_private_view(state)
    assert "hole" not in view(state, None)["seats"][0]
    assert all("hole" in player for player in view(state, None)["seats"][1:])
    saved = deepcopy(state)
    snapshot = view(state, "p0")
    snapshot["seats"][0]["hole"].clear()
    snapshot["board"].clear()
    snapshot["pots"][0]["eligible"].clear()
    snapshot["results"]["players"]["p1"]["best5"].clear()
    outcome = results(state)
    outcome["players"]["p1"]["best5"].clear()
    assert state == saved


@pytest.mark.parametrize(("hole", "stack", "expected"), [
    ("As Ah", 100, "call"), ("7s 7h", 100, "call"),
    ("As Kh", 100, "call"), ("As Qh", 100, "call"), ("Ks Qh", 100, "call"),
    ("As Jh", 100, "fold"), ("Js Th", 100, "fold"), ("3s 2h", 100, "fold"),
    ("As Ah", 10, "fold"), ("As Kh", 7, "fold"),
])
def test_auto_preflop_pairs_premium_cards_and_never_all_in_call(hole, stack, expected):
    state = rigged([hole, "9c 8d"], "2c 4d 6c 7d Tc", [stack, 100])
    assert auto_action(state, "p0") == (expected, None)
    act(state, *auto_action(state, "p0"))


def test_auto_preflop_pair_still_folds_if_call_exceeds_half_pot():
    state = rigged(["As Ah", "9c 8d", "Ks Qh"], "2c 4d 6c 7d Tc")
    assert legal_actions(state, "p0")["call"] == 10  # Pot is only 15.
    assert auto_action(state, "p0") == ("fold", None)


@pytest.mark.parametrize(("hole", "board", "stack", "bet", "expected"), [
    ("As Kc", "Ah 7s 2d 9c Td", 100, 10, "call"),
    ("As Kc", "Ah 7s 2d 9c Td", 100, 20, "call"),  # Exactly half the pot.
    ("As Kc", "Ah 7s 2d 9c Td", 100, 21, "fold"),
    ("Ks Qc", "Ah 7s 2d 9c Td", 100, 10, "fold"),
    ("As Kc", "Ah 7s 2d 9c Td", 20, 10, "fold"),  # One pair, all-in call.
    ("As Kc", "Ah 7s 7d 9c Td", 20, 10, "call"),  # Two pairs, all-in call.
    ("As Kc", "Ah 7s 7d 9c Td", 14, 10, "call"),  # Partial all-in call.
    ("7s Kc", "Ah 7h 7d 9c Td", 14, 10, "call"),
    ("As Kc", "7h 7s 2d 9c Td", 100, 10, "call"),  # Board pair is a made hand.
])
def test_auto_postflop_price_and_all_in_made_hand_threshold(hole, board, stack, bet, expected):
    state = rigged([hole, "Jh 8d"], board, [stack, 200])
    state = act(state, "call")
    state = act(state, "check")
    assert auto_action(state, "p1") == ("check", None)
    state = act(state, "bet", bet, player="p1")
    assert auto_action(state, "p0") == (expected, None)
    act(state, *auto_action(state, "p0"))


def test_auto_always_checks_free_and_rejects_off_turn():
    state = act(start([100, 100]), "call")
    assert auto_action(state, "p1") == ("check", None)
    with pytest.raises(EngineError):
        auto_action(state, "p0")
    with pytest.raises(EngineError):
        auto_action(check_down(state), "p0")


def test_new_hand_and_apply_are_immutable_and_input_order_independent():
    participants = seats([100, 200, 300])
    deck = list(range(52))
    participants_before, deck_before = deepcopy(participants), list(deck)
    state = new_hand(seats=participants, button=0, small_blind=5, big_blind=10, deck=deck)
    assert participants == participants_before and deck == deck_before
    assert new_hand(seats=list(reversed(participants)), button=0, small_blind=5,
                    big_blind=10, deck=deck) == state
    saved = deepcopy(state)
    updated = act(state, "call")
    assert state == saved
    updated["seats"][0]["hole"].clear()
    updated["deck"].clear()
    assert state == saved
    deck.clear()
    participants[0]["stack"] = 1
    assert state == saved


def test_same_deck_and_action_sequence_are_completely_deterministic():
    initial = rigged(["As Ah", "Ks Kh", "Qs Qh", "Js Jh"], "2s 3h 7d 8c 9s",
                     [50, 100, 200, 300])
    commands = [("p3", "all_in", None), ("p0", "call", None),
                ("p1", "call", None), ("p2", "call", None)]
    left, right = deepcopy(initial), json.loads(json.dumps(initial))
    for player, action, amount in commands:
        left = apply_action(left, player, action, amount)
        right = json.loads(json.dumps(apply_action(right, player, action, amount)))
        assert left == right
    assert results(left) == results(right)
    assert view(left, None) == view(right, None)


@pytest.mark.parametrize("patch", [
    {"seats": []}, {"seats": seats([1])}, {"seats": seats([1] * 7)},
    {"seats": [{"seat": 0, "player": "p0", "stack": 0}, {"seat": 1, "player": "p1", "stack": 1}]},
    {"seats": [{"seat": 0, "player": "p0", "stack": True}, {"seat": 1, "player": "p1", "stack": 1}]},
    {"seats": [{"seat": 0, "player": "p0", "stack": 1}, {"seat": 0, "player": "p1", "stack": 1}]},
    {"seats": [{"seat": 0, "player": "same", "stack": 1}, {"seat": 1, "player": "same", "stack": 1}]},
    {"seats": [{"seat": True, "player": "p0", "stack": 1}, {"seat": 1, "player": "p1", "stack": 1}]},
    {"seats": [{"seat": 0, "player": "", "stack": 1}, {"seat": 1, "player": "p1", "stack": 1}]},
    {"seats": [None, None]}, {"button": 5}, {"button": True},
    {"small_blind": 0}, {"small_blind": 11}, {"big_blind": -1}, {"big_blind": 1.5},
    {"deck": list(range(51))}, {"deck": [0] * 52}, {"deck": list(range(1, 53))},
    {"deck": [False] + list(range(1, 52))},
])
def test_invalid_new_hand_requests_have_chinese_engine_errors(patch):
    kwargs = {"seats": seats([100, 100, 100]), "button": 0,
              "small_blind": 5, "big_blind": 10, "deck": list(range(52))}
    kwargs.update(patch)
    with pytest.raises(EngineError, match="[\u4e00-\u9fff]"):
        new_hand(**kwargs)


@pytest.mark.parametrize(("action", "amount"), [
    ("check", None), ("bet", 20), ("raise", None), ("raise", True), ("raise", 20.0),
    ("raise", 19), ("raise", 201), ("raise", -1), ("fold", 1), ("all_in", 200),
    ("dance", None),
])
def test_invalid_actions_do_not_change_state(action, amount):
    state = start()
    before = deepcopy(state)
    with pytest.raises(EngineError, match="[\u4e00-\u9fff]"):
        act(state, action, amount)
    assert state == before


def test_off_turn_finished_and_unopened_action_validation():
    state = start()
    assert legal_actions(state, "p1") is None
    assert legal_actions(state, "unknown") is None
    with pytest.raises(EngineError):
        apply_action(state, "p1", "fold")
    with pytest.raises(EngineError):
        results(state)
    for _ in range(3):
        opts = legal_actions(state, current_player(state))
        state = act(state, "check" if opts["check"] else "call")
    assert state["street"] == "flop"
    for action, amount in (("call", None), ("raise", 10)):
        with pytest.raises(EngineError):
            act(state, action, amount)
    state = check_down(state)
    assert current_player(state) is None
    assert legal_actions(state, "p0") is None
    with pytest.raises(EngineError):
        apply_action(state, "p0", "check")


@pytest.mark.parametrize("hand", [[], [0] * 5, [0, 1, 2, 3, 52], [False, 1, 2, 3, 4],
                                  [0, 1, 2, 3, 4.0], list(range(8))])
def test_invalid_hand_evaluation(hand):
    with pytest.raises(EngineError):
        best_hand(hand)


def test_invalid_hints():
    for hole, board in [([0], []), ([0, 1], [2]), ([0, 1], [0, 2, 3])]:
        with pytest.raises(EngineError):
            hand_hint(hole, board)


def test_10000_seeded_random_hands_serialization_legality_conservation_and_auto():
    rng = random.Random(20261025)
    auto_count = 0
    street_visits = Counter()
    action_visits = Counter()
    for hand_number in range(10000):
        count = rng.randint(2, 6)
        positions = rng.sample(range(6), count)
        stacks = [rng.randint(1, 300) for _ in positions]
        big = rng.randint(1, 30)
        small = rng.randint(1, big)
        button = rng.choice(positions)
        deck = list(range(52))
        rng.shuffle(deck)
        state = start(stacks, positions=positions, button=button, small=small, big=big, deck=deck)
        history = []
        try:
            while True:
                restored = assert_integrity(state, sum(stacks))
                street_visits[state["street"]] += 1
                if is_finished(state):
                    break
                assert len(history) < 2000
                player = current_player(state)
                options = legal_actions(state, player)
                if rng.random() < 0.25:
                    action, amount = auto_action(state, player)
                    assert (action, amount) == auto_action(restored, player)
                    assert action in ("fold", "check", "call") and amount is None
                    auto_count += 1
                else:
                    choices = [("fold", None)]
                    if options["check"]:
                        choices += [("check", None)] * 6
                    if options["call"]:
                        choices += [("call", None)] * 6
                    if options["all_in"]:
                        choices.append(("all_in", None))
                    if options["min_raise_to"] is not None:
                        minimum, maximum = options["min_raise_to"], options["max_raise_to"]
                        raising = "bet" if state["current_bet"] == 0 else "raise"
                        choices += [(raising, minimum), (raising, rng.randint(minimum, maximum))]
                    action, amount = rng.choice(choices)
                history.append((player, action, amount))
                action_visits[action] += 1
                updated = apply_action(state, player, action, amount)
                assert state == restored  # Successful actions must not mutate their input.
                assert updated == apply_action(restored, player, action, amount)
                state = updated
        except Exception as exc:
            exc.add_note(f"seed=20261025 hand={hand_number}, seats={positions}, stacks={stacks}, "
                         f"blinds={small}/{big}, button={button}, deck={deck}, actions={history}")
            raise
    assert auto_count > 10000
    assert set(street_visits) == {"preflop", "flop", "turn", "river"}
    assert set(action_visits) == {"fold", "check", "call", "bet", "raise", "all_in"}
