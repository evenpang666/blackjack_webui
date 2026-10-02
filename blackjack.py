"""Server-authoritative single-deck blackjack. All accounting uses integer cents."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_PLAYERS = 5
IDLE = ("waiting", "complete")


def money(cents):
    return cents / 100


def cents(value):
    if type(value) not in (int, float):
        raise ValueError("下注金额无效")
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError("金额最多保留两位小数")
        return int(amount)
    except InvalidOperation:
        raise ValueError("下注金额无效") from None


def deck():
    cards = [(rank, suit) for suit in "shdc" for rank in range(2, 15)]
    random.SystemRandom().shuffle(cards)
    return cards


def card_text(card):
    return "23456789TJQKA"[card[0] - 2] + card[1]


def value(cards):
    total = sum(11 if rank == 14 else min(rank, 10) for rank, _ in cards)
    aces = sum(rank == 14 for rank, _ in cards)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, bool(aces)


@dataclass
class Hand:
    cards: list = field(default_factory=list)
    bet: int = 0
    status: str = "playing"
    split: bool = False
    split_aces: bool = False
    payout: int = 0
    outcome: str = ""

    @property
    def blackjack(self):
        return not self.split and len(self.cards) == 2 and value(self.cards)[0] == 21


@dataclass
class Player:
    id: str
    name: str
    stack: int
    buyin: int
    connected: bool = True
    departed: bool = False
    pending_buyin: int = 0
    in_hand: bool = False
    hands: list[Hand] = field(default_factory=list)
    bet_decided: bool = False
    insurance_decided: bool = False
    insurance: int = 0
    insurance_payout: int = 0
    round_start: int = 0
    last_action: str = ""


class Table:
    def __init__(self):
        self.players: list[Player] = []
        self.phase = "waiting"
        self.turn = None
        self.hand_index = 0
        self.hand_no = 0
        self.dealer_cards = []
        self.pile = []
        self.events = []
        self.result = None
        self.house_net = 0

    def add(self, player):
        if sum(not p.departed for p in self.players) >= MAX_PLAYERS:
            raise ValueError("房间已满（闲家最多 5 人）")
        if not any(p is player for p in self.players):
            self.players.append(player)

    def eligible(self):
        return [p for p in self.players if not p.departed and p.connected and p.stack > 0]

    def start(self):
        if self.phase not in IDLE:
            raise ValueError("当前牌局尚未结束")
        if not self.eligible():
            raise ValueError("至少需要一位在线且有筹码的玩家")
        self.players = [p for p in self.players if not p.departed]
        self.phase = "betting"
        self.hand_no += 1
        self.turn = None
        self.hand_index = 0
        self.dealer_cards = []
        self.pile = deck()
        self.result = None
        ready = self.eligible()
        for p in self.players:
            p.in_hand = p in ready
            p.hands = []
            p.bet_decided = not p.in_hand
            p.insurance_decided = False
            p.insurance = p.insurance_payout = 0
            p.round_start = p.stack
            p.last_action = "等待下注" if p.in_hand else "等待下一局"
        self.events.append(f"第 {self.hand_no} 局开始，请先下注；单副 52 张牌已洗牌")

    def bet(self, pid, amount=None):
        p = self.player(pid)
        if self.phase != "betting" or not p.in_hand or p.bet_decided:
            raise ValueError("当前不能下注")
        paid = 0 if amount is None else cents(amount)
        if amount is not None and (paid <= 0 or paid > p.stack or
                                  (paid % 500 and paid != p.stack)):
            raise ValueError("下注须为 5 的倍数且不超过筹码，或投入全部剩余筹码")
        p.bet_decided = True
        p.stack -= paid
        p.hands = [Hand(bet=paid)] if paid else []
        p.last_action = f"下注 ¥{money(paid):g}" if paid else "本局旁观"
        self.events.append(f"{p.name} {p.last_action}")
        self.finish_betting()

    def finish_betting(self):
        if any(p.in_hand and not p.bet_decided for p in self.players):
            return
        participants = [p for p in self.players if p.hands]
        if not participants:
            self.phase = "complete"
            self.result = {"players": [], "dealer": "无人下注", "houseNet": 0}
            self.events.append("无人下注，本局结束")
            return
        for _ in range(2):
            for p in participants:
                p.hands[0].cards.append(self.draw())
            self.dealer_cards.append(self.draw())
        self.events.append("初始发牌完成，庄家暗牌保密")
        if self.dealer_cards[0][0] == 14:
            self.phase = "insurance"
            self.advance_insurance()
        else:
            self.check_dealer()

    def draw(self):
        # A round uses at most 52 unique cards. If exhausted, void and refund
        # the whole round rather than introducing another deck or duplicate cards.
        return self.pile.pop()

    def player(self, pid):
        p = next((p for p in self.players if p.id == pid and not p.departed), None)
        if p is None:
            raise ValueError("玩家不在房间内")
        return p

    def advance_insurance(self):
        for p in self.players:
            if p.hands and not p.insurance_decided:
                if p.departed or not p.connected:
                    p.insurance_decided = True
                    continue
                self.turn = p.id
                return
        self.turn = None
        self.check_dealer()

    def check_dealer(self):
        natural = len(self.dealer_cards) == 2 and value(self.dealer_cards)[0] == 21
        for p in self.players:
            if p.insurance:
                p.insurance_payout = p.insurance * 3 if natural else 0
                p.stack += p.insurance_payout
                self.events.append(f"{p.name} 保险{'赔付' if natural else '未中'} ¥{money(p.insurance_payout):g}")
        if natural:
            self.settle()
            return
        self.phase = "playing"
        for p in self.players:
            for h in p.hands:
                if h.blackjack:
                    h.status = "blackjack"
        self.advance()

    def advance(self):
        self.turn = None
        for p in self.players:
            for i, h in enumerate(p.hands):
                if h.status == "playing":
                    if p.departed or not p.connected:
                        h.status = "stand"
                        p.last_action = "离线自动停牌"
                        self.events.append(f"{p.name} 第 {i + 1} 手自动停牌")
                        continue
                    self.turn, self.hand_index = p.id, i
                    return
        self.phase = "dealer"

    def options(self, p):
        if p.departed:
            return None
        if self.phase == "betting" and p.in_hand and not p.bet_decided:
            return {"bet": True, "maxBet": money(p.stack)}
        if self.turn != p.id:
            return None
        if self.phase == "insurance":
            limit = p.hands[0].bet // 2
            return {"insurance": p.stack >= limit and limit > 0,
                    "insuranceAmount": money(limit), "decline": True}
        if self.phase != "playing":
            return None
        h = p.hands[self.hand_index]
        two = len(h.cards) == 2 and not h.split_aces
        # Aces are distinct from ten-valued cards.
        pair = two and (h.cards[0][0] == h.cards[1][0] or
                        all(10 <= c[0] <= 13 for c in h.cards))
        return {"hit": True, "stand": True, "double": two and p.stack >= h.bet,
                "split": pair and len(p.hands) < 4 and p.stack >= h.bet,
                "surrender": two and not h.split, "handIndex": self.hand_index}

    def act(self, pid, action, amount=None):
        if type(action) is not str:
            raise ValueError("操作必须是字符串")
        p = self.player(pid)
        if action in ("bet", "skip"):
            return self.bet(pid, amount if action == "bet" else None)
        opt = self.options(p)
        if not opt or not opt.get(action):
            raise ValueError("当前不能执行此操作，或现在不是你的回合")
        if self.phase == "insurance":
            p.insurance_decided = True
            if action == "insurance":
                p.insurance = p.hands[0].bet // 2
                p.stack -= p.insurance
            p.last_action = "购买保险" if p.insurance else "不买保险"
            self.events.append(f"{p.name} {p.last_action}")
            self.advance_insurance()
            return
        h = p.hands[self.hand_index]
        needed = 2 if action == "split" else 1 if action in ("hit", "double") else 0
        if len(self.pile) < needed:
            self.void()
            return
        labels = {"hit": "要牌", "stand": "停牌", "double": "加倍", "split": "分牌", "surrender": "投降"}
        p.last_action = labels[action]
        self.events.append(f"{p.name} 第 {self.hand_index + 1} 手{p.last_action}")
        if action == "stand":
            h.status = "stand"
        elif action == "surrender":
            h.status = "surrender"
        elif action in ("hit", "double"):
            if action == "double":
                p.stack -= h.bet
                h.bet *= 2
            h.cards.append(self.draw())
            total = value(h.cards)[0]
            if total > 21:
                h.status = "bust"
            elif total == 21 or action == "double":
                h.status = "stand"
        elif action == "split":
            p.stack -= h.bet
            aces = h.cards[0][0] == 14
            first = Hand([h.cards[0], self.draw()], h.bet, split=True, split_aces=aces)
            second = Hand([h.cards[1], self.draw()], h.bet, split=True, split_aces=aces)
            for hand in (first, second):
                if aces or value(hand.cards)[0] == 21:
                    hand.status = "stand"
            p.hands[self.hand_index:self.hand_index + 1] = [first, second]
        self.advance()

    def dealer_step(self):
        if self.phase != "dealer":
            return
        need_play = any(h.status not in ("bust", "surrender", "blackjack")
                        for p in self.players for h in p.hands)
        if need_play and value(self.dealer_cards)[0] < 17:
            if not self.pile:
                self.void()
                return
            self.dealer_cards.append(self.draw())
            self.events.append(f"电脑庄家要牌，当前 {value(self.dealer_cards)[0]} 点")
            return
        self.settle()

    def settle(self):
        total = value(self.dealer_cards)[0]
        natural = len(self.dealer_cards) == 2 and total == 21
        rows = []
        for p in self.players:
            if not p.hands:
                continue
            for h in p.hands:
                points = value(h.cards)[0]
                if h.status == "surrender":
                    h.payout, h.outcome = h.bet // 2, "投降"
                elif points > 21:
                    h.payout, h.outcome = 0, "爆牌"
                elif h.blackjack and natural:
                    h.payout, h.outcome = h.bet, "平局"
                elif natural:
                    h.payout, h.outcome = 0, "庄家 Blackjack"
                elif h.blackjack:
                    h.payout, h.outcome = h.bet + (h.bet * 3 + 1) // 2, "Blackjack"
                elif total > 21 or points > total:
                    h.payout, h.outcome = h.bet * 2, "获胜"
                elif points == total:
                    h.payout, h.outcome = h.bet, "平局"
                else:
                    h.payout, h.outcome = 0, "落败"
                p.stack += h.payout
                h.status = "complete"
            net = p.stack - p.round_start
            rows.append({"id": p.id, "name": p.name, "net": money(net),
                         "outcomes": [h.outcome for h in p.hands]})
            self.events.append(f"{p.name} {' / '.join(h.outcome for h in p.hands)}，本局净输赢 ¥{money(net):+g}")
        house = -sum(p.stack - p.round_start for p in self.players if p.hands)
        self.house_net += house
        self.result = {"players": rows, "dealer": "Blackjack" if natural else f"{total} 点" if total <= 21 else "爆牌",
                       "houseNet": money(house)}
        self.events.append(f"本局结束，电脑庄家 {self.result['dealer']}")
        self.phase, self.turn = "complete", None

    def void(self):
        for p in self.players:
            if p.hands:
                p.stack = p.round_start
                for h in p.hands:
                    h.status, h.outcome = "complete", "退还下注"
        self.phase, self.turn = "complete", None
        self.result = {"players": [], "dealer": "单副牌耗尽，本局作废并退还所有下注", "houseNet": 0}
        self.events.append(self.result["dealer"])

    def disconnect(self, pid):
        p = next((p for p in self.players if p.id == pid), None)
        if p is None:
            return
        p.connected = False
        if self.phase == "betting" and p.in_hand and not p.bet_decided:
            p.bet_decided = True
            p.last_action = "离线旁观"
            self.finish_betting()
        elif self.phase == "insurance":
            p.insurance_decided = True
            self.advance_insurance()
        elif self.phase == "playing":
            for h in p.hands:
                if h.status == "playing":
                    h.status = "stand"
            p.last_action = "离线自动停牌"
            self.advance()

    def remove(self, pid, label="离开房间"):
        p = self.player(pid)
        self.disconnect(pid)
        p.departed = True
        self.events.append(f"{p.name} {label}")

    def view(self, viewer):
        visible = self.phase in ("dealer", "complete")
        players = []
        for p in self.players:
            if p.departed:
                continue
            hands = [{"cards": [card_text(c) for c in h.cards], "bet": money(h.bet),
                      "total": value(h.cards)[0], "soft": value(h.cards)[1],
                      "status": h.status, "blackjack": h.blackjack, "outcome": h.outcome,
                      "payout": money(h.payout)} for h in p.hands]
            players.append({"id": p.id, "name": p.name, "stack": money(p.stack),
                            "buyin": money(p.buyin), "connected": p.connected,
                            "inHand": p.in_hand, "hands": hands,
                            "lastAction": p.last_action, "pendingBuyin": money(p.pending_buyin),
                            "insurance": money(p.insurance),
                            "bankrupt": p.stack == 0 and self.phase in IDLE})
        my = next((p for p in self.players if p.id == viewer), None)
        return {"phase": self.phase, "handNo": self.hand_no, "turn": self.turn,
                "handIndex": self.hand_index, "players": players,
                "dealerCards": [card_text(c) if visible or i == 0 else "??"
                                for i, c in enumerate(self.dealer_cards)],
                "dealerTotal": value(self.dealer_cards)[0] if visible else None,
                "cardsLeft": len(self.pile), "houseNet": money(self.house_net),
                "result": self.result, "options": self.options(my) if my else None}
