from unittest.mock import patch

import pytest

from blackjack import Hand, Player, Table, deck, value


def cards(*ranks):
    return [(rank, "shdc"[i % 4]) for i, rank in enumerate(ranks)]


def table_with(*hands, dealer=(10, 7), balance=10000, bet=1000):
    t = Table()
    for i, hand in enumerate(hands):
        p = Player(str(i), f"玩家{i}", balance, balance)
        t.add(p)
    t.start()
    sequence = [c for deal in range(2) for c in
                [*(hand[deal] for hand in hands), cards(*dealer)[deal]]]
    t.pile = list(reversed(sequence))
    for p in t.players:
        t.bet(p.id, bet / 100)
    return t


def finish(t):
    for _ in range(100):
        if t.phase == "complete":
            return
        if t.phase == "insurance":
            t.act(t.turn, "decline")
        elif t.phase == "playing":
            t.act(t.turn, "stand")
        elif t.phase == "dealer":
            t.dealer_step()
        else:
            raise AssertionError(t.phase)
    raise AssertionError("牌局未能结束")


@pytest.mark.parametrize("ranks,total,soft", [
    ((14, 6), 17, True), ((14, 14, 9), 21, True),
    ((14, 14, 9, 10), 21, False), ((13, 12, 2), 22, False),
    ((14, 14, 14, 8), 21, True), ((14, 10, 10), 21, False),
])
def test_ace_values(ranks, total, soft):
    assert value(cards(*ranks)) == (total, soft)


def test_single_deck_and_capacity():
    assert len(deck()) == len(set(deck())) == 52
    t = Table()
    for i in range(5):
        t.add(Player(str(i), str(i), 50000, 50000))
    with pytest.raises(ValueError, match="5 人"):
        t.add(Player("6", "6", 50000, 50000))
    t.start()
    for p in t.players:
        t.bet(p.id, 10)
    live = [c for p in t.players for h in p.hands for c in h.cards] + t.dealer_cards + t.pile
    assert len(live) == len(set(live)) == 52


@pytest.mark.parametrize("ranks,dealer,net,outcome", [
    ((14, 10), (10, 7), 1500, "Blackjack"),
    ((14, 10), (14, 10), 0, "平局"),
    ((10, 9), (10, 7), 1000, "获胜"),
    ((10, 7), (10, 7), 0, "平局"),
    ((10, 6), (10, 7), -1000, "落败"),
    ((10, 9), (14, 10), -1000, "庄家 Blackjack"),
])
def test_payouts(ranks, dealer, net, outcome):
    t = table_with(cards(*ranks), dealer=dealer)
    finish(t)
    assert t.players[0].stack == 10000 + net
    assert t.players[0].hands[0].outcome == outcome
    assert t.house_net == -net


def test_soft17_and_hole_card_privacy():
    t = table_with(cards(10, 8), dealer=(14, 6))
    assert t.view("0")["dealerCards"] == ["As", "??"]
    assert t.view("0")["dealerTotal"] is None
    t.act("0", "decline")
    t.act("0", "stand")
    assert t.view("0")["dealerCards"] == ["As", "6h"]
    t.dealer_step()
    assert t.phase == "complete"
    assert len(t.dealer_cards) == 2


def test_dealer_hits_and_player_bust_loses_even_if_dealer_busts():
    t = table_with(cards(10, 9), cards(10, 8), dealer=(10, 6))
    t.pile = list(reversed(cards(5, 10)))
    t.act("0", "hit")
    assert t.turn == "1"
    t.act("1", "stand")
    t.dealer_step()
    t.dealer_step()
    assert t.players[0].stack == 9000
    assert t.players[1].stack == 11000


@pytest.mark.parametrize("dealer,stack", [((14, 10), 10000), ((14, 7), 8500)])
def test_insurance_independent_settlement(dealer, stack):
    t = table_with(cards(10, 6), dealer=dealer)
    assert t.options(t.players[0])["insuranceAmount"] == 5
    t.act("0", "insurance")
    finish(t)
    assert t.players[0].stack == stack
    assert t.house_net == 10000 - stack


def test_natural_blackjack_insurance_and_insufficient_funds():
    t = table_with(cards(14, 10), dealer=(14, 10))
    t.act("0", "insurance")
    assert t.players[0].stack == 11000
    t = table_with(cards(8, 8), dealer=(14, 10), balance=1000)
    assert not t.options(t.players[0])["insurance"]
    with pytest.raises(ValueError):
        t.act("0", "insurance")
    t.act("0", "decline")
    assert t.phase == "complete"
    assert t.players[0].stack == 0


def test_double_takes_one_card_and_finishes():
    t = table_with(cards(5, 6))
    t.pile = cards(10)
    t.act("0", "double")
    assert t.phase == "dealer"
    assert t.players[0].hands[0].bet == 2000
    finish(t)
    assert t.players[0].stack == 12000


def test_split_then_double_and_resplit_limit():
    t = table_with(cards(8, 8))
    t.pile = list(reversed(cards(3, 8, 10)))
    t.act("0", "split")
    assert len(t.players[0].hands) == 2
    assert t.hand_index == 0
    t.act("0", "double")
    assert t.hand_index == 1
    t.pile = list(reversed(cards(8, 8, 8, 8)))
    t.act("0", "split")
    t.act("0", "split")
    assert len(t.players[0].hands) == 4
    assert not t.options(t.players[0])["split"]
    assert not t.options(t.players[0])["surrender"]
    finish(t)


def test_split_aces_one_card_and_no_blackjack_payout():
    t = table_with(cards(14, 14))
    t.pile = list(reversed(cards(10, 10)))
    t.act("0", "split")
    assert t.phase == "dealer"
    assert all(len(h.cards) == 2 and not h.blackjack for h in t.players[0].hands)
    finish(t)
    assert t.players[0].stack == 12000


def test_ten_values_can_split_and_different_ranks_cannot():
    t = table_with(cards(10, 13))
    assert t.options(t.players[0])["split"]
    t = table_with(cards(7, 8))
    assert not t.options(t.players[0])["split"]
    t = table_with(cards(14, 10))
    assert t.phase == "dealer"


def test_late_surrender_and_no_surrender_after_hit():
    t = table_with(cards(10, 6))
    t.act("0", "surrender")
    finish(t)
    assert t.players[0].stack == 9500
    t = table_with(cards(5, 6))
    t.pile = cards(2)
    t.act("0", "hit")
    assert not t.options(t.players[0])["surrender"]
    assert not t.options(t.players[0])["double"]


def test_dealer_blackjack_prevents_double_split_and_surrender():
    t = table_with(cards(8, 8), dealer=(10, 14))
    assert t.phase == "complete"
    for action in ("double", "split", "surrender"):
        with pytest.raises(ValueError):
            t.act("0", action)


def test_illegal_actions_are_atomic_and_exhaustion_refunds():
    t = table_with(cards(7, 8), balance=1000)
    snapshot = t.view("0")
    for action in ("split", "double", "insurance", "hack", "fold"):
        with pytest.raises(ValueError):
            t.act("0", action)
        assert t.view("0") == snapshot
    with pytest.raises(ValueError):
        t.act("other", "hit")
    t.act("0", "hit")
    assert t.phase == "complete"
    assert t.players[0].stack == 1000
    assert t.house_net == 0


@pytest.mark.parametrize("amount", [-5, 0, 6, 200, True, "10", [], float("nan"), 5.001])
def test_invalid_bets(amount):
    t = Table()
    p = Player("p", "p", 10000, 10000)
    t.add(p)
    t.start()
    with pytest.raises(ValueError):
        t.bet("p", amount)
    assert p.stack == 10000 and not p.bet_decided


def test_fractional_balance_can_go_all_in_and_exact_cent_accounting():
    t = table_with(cards(14, 10), bet=500)
    finish(t)
    assert t.players[0].stack == 10750
    t = table_with(cards(14, 10), balance=1, bet=1)
    finish(t)
    assert t.players[0].stack == 3
    assert t.house_net == -2


def test_late_join_disconnect_and_departed_payout():
    t = table_with(cards(10, 9), cards(10, 8))
    late = Player("late", "late", 10000, 10000)
    t.add(late)
    assert t.options(late) is None
    t.remove("0")
    t.disconnect("1")
    assert t.phase == "dealer"
    finish(t)
    assert t.players[0].stack == 11000
    assert t.players[1].stack == 11000
    assert late.stack == 10000
    assert t.house_net == -2000


def test_disconnect_during_betting_and_no_one_bets():
    t = Table()
    t.add(Player("p", "p", 10000, 10000))
    t.start()
    t.disconnect("p")
    assert t.phase == "complete"
    assert not t.dealer_cards
    assert t.players[0].stack == 10000


def test_random_rounds_conserve_chips_and_use_only_one_deck():
    import random
    rng = random.Random(21)
    t = Table()
    for i in range(5):
        t.add(Player(str(i), str(i), 100000, 100000))
    for _ in range(150):
        if not t.eligible():
            break
        t.start()
        for p in list(t.players):
            if t.options(p):
                t.bet(p.id, 5 if p.stack >= 500 else p.stack / 100)
        for _ in range(300):
            if t.phase == "complete":
                break
            if t.phase == "dealer":
                t.dealer_step()
            else:
                p = t.player(t.turn)
                opt = t.options(p)
                actions = [a for a in ("hit", "stand", "double", "split", "surrender", "insurance", "decline") if opt.get(a)]
                t.act(p.id, rng.choice(actions))
            live = [c for p in t.players for h in p.hands for c in h.cards] + t.dealer_cards + t.pile
            assert len(live) == len(set(live)) == 52
        assert t.phase == "complete"
        assert all(p.stack >= 0 for p in t.players)
        assert sum(p.stack for p in t.players) + t.house_net == sum(p.buyin for p in t.players)
