"""
키움 없이 전체 흐름을 점검하는 테스트 (리눅스/맥/윈도우 어디서나)
실행: python test_flow.py
"""
import datetime as dt
import os
import shutil
import tempfile

import numpy as np
import pandas as pd

import common
import config

# 테스트 전용 폴더 사용 (실제 data/ 를 건드리지 않음)
TMP = tempfile.mkdtemp()
common.DATA = TMP
common.STATE_FILE = os.path.join(TMP, "state.json")
common.TRADES_FILE = os.path.join(TMP, "trades.csv")

import scan            # noqa: E402
import trader_core     # noqa: E402

for mod in (scan, trader_core):          # 같은 이름으로 import된 함수들도 테스트 폴더를 보게
    for fn in ("load_state", "save_state", "save_signals", "latest_signals", "record_trade"):
        if hasattr(mod, fn):
            setattr(mod, fn, getattr(common, fn))


class FakeBroker:
    def __init__(self, cash=100_000_000, mock=True, open_day=True):
        self.account = "TEST-01"
        self.cash = cash
        self.mock = mock
        self.open_day = open_day
        self.hold = {}          # code → [qty, avg]
        self.queue = []         # 아직 체결 안 된 장전 주문
        self.price = {}
        self.subscribed = []
        self.quit_called = False
        self.on_price = self.on_fill = None

    def is_mock(self):
        return self.mock

    def balance(self):
        val = sum(q * self.price.get(c, a) for c, (q, a) in self.hold.items())
        h = {c: {"qty": q, "avg": a, "cur": self.price.get(c, a), "name": c} for c, (q, a) in self.hold.items()}
        return self.cash + val, self.cash, h

    def market_open(self):
        return self.open_day

    def order(self, side, code, qty):
        self.queue.append((side, code, qty))
        return True

    def open_auction(self, open_prices):
        """09:00 동시호가 체결"""
        if not self.open_day:
            return
        for side, code, qty in self.queue:
            self._fill(side, code, qty, open_prices[code])
        self.queue = []

    def _fill(self, side, code, qty, px):
        if side == "buy":
            q, a = self.hold.get(code, [0, 0])
            self.hold[code] = [q + qty, round((q * a + qty * px) / (q + qty))]
            self.cash -= qty * px
        else:
            self.hold[code][0] -= qty
            self.cash += qty * px
            if self.hold[code][0] <= 0:
                del self.hold[code]
        self.on_fill(code, side, px, qty)

    def subscribe(self, codes):
        self.subscribed = codes

    def tick_price(self, code, px):
        """장중 시세 → 손절 판단 → 시장가면 즉시 체결"""
        self.price[code] = px
        self.on_price(code, px)
        for side, c, qty in list(self.queue):
            if c == code and side == "sell":
                self._fill(side, c, qty, px)
                self.queue.remove((side, c, qty))

    def quit(self):
        self.quit_called = True


def run_day(trader, broker, day, opens, intraday=()):
    for hhmm in ["08:20", "08:45", "08:59"]:
        trader.tick(dt.datetime.combine(day, dt.time(*map(int, hhmm.split(":")))))
    broker.open_auction(opens)
    trader.tick(dt.datetime.combine(day, dt.time(9, 3)))
    for code, px in intraday:
        broker.tick_price(code, px)
    trader.tick(dt.datetime.combine(day, dt.time(9, 10)))
    trader.tick(dt.datetime.combine(day, dt.time(15, 25)))


def make_prices(end):
    """A,B: 52주 신고가 근처 상승추세 / C: 하락추세 / D: 거래대금 부족"""
    idx = pd.bdate_range(end=end, periods=300)
    def series(trend, base, vol):
        c = base * np.exp(np.linspace(0, trend, len(idx)))
        return pd.DataFrame({"Open": c * 0.995, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": vol}, index=idx)
    return {"000010": series(0.8, 10000, 2_000_000), "000020": series(0.5, 30000, 500_000),
            "000030": series(-0.5, 10000, 2_000_000), "000040": series(0.8, 2000, 10_000)}


def check(cond, msg):
    print(("  통과  " if cond else "  실패! ") + msg)
    assert cond, msg


def main():
    config.DRY_RUN = False
    config.IS_REAL = False
    uni = pd.DataFrame({"Code": ["000010", "000020", "000030", "000040"],
                        "Name": ["상승A", "상승B", "하락C", "소형D"], "Market": ["KOSPI"] * 4})
    d0 = dt.date(2026, 9, 25)    # 금요일
    prices = make_prices(d0)
    mk = {"KOSPI": (True, pd.Timestamp(d0)), "KOSDAQ": (True, pd.Timestamp(d0))}

    print("[1] 스캐너: 조건 맞는 종목만 매수후보")
    sig = scan.run(universe=uni, prices=prices, market=mk, now=dt.datetime.combine(d0, dt.time(16, 0)))
    codes = [b["code"] for b in sig["buys"]]
    check(codes == ["000010", "000020"], f"매수후보 {codes} (거래대금 큰 순)")

    print("[2] 시장필터 미통과면 후보 없음")
    mk_bad = {"KOSPI": (False, pd.Timestamp(d0)), "KOSDAQ": (False, pd.Timestamp(d0))}
    check(scan.run(universe=uni, prices=prices, market=mk_bad,
                   now=dt.datetime.combine(d0, dt.time(16, 0)))["buys"] == [], "후보 0개")
    scan.run(universe=uni, prices=prices, market=mk, now=dt.datetime.combine(d0, dt.time(16, 0)))

    print("[3] 실전 서버 로그인 + IS_REAL=False → 주문 없이 종료")
    b = FakeBroker(mock=False)
    t = trader_core.Trader(b)
    t.tick(dt.datetime(2026, 9, 28, 8, 20))
    check(b.quit_called and not b.queue, "안전장치 작동")

    print("[4] 월요일: 아침 매수 → 체결 → 손절")
    d1 = dt.date(2026, 9, 28)
    b = FakeBroker()
    t = trader_core.Trader(b)
    opens = {"000010": float(prices["000010"]["Close"].iloc[-1] * 1.01),
             "000020": float(prices["000020"]["Close"].iloc[-1])}
    run_day(t, b, d1, opens, intraday=[("000020", opens["000020"] * 0.97),
                                       ("000020", opens["000020"] * 0.91)])
    st = common.load_state()
    check(set(b.hold) == {"000010"}, f"보유 {list(b.hold)} (B는 -9%에서 손절)")
    p = st["positions"]["000010"]
    check(p["status"] == "보유" and p["entry_price"] == round(opens["000010"]), "A 매수가 = 실제 체결가")
    check(p["stop"] == int(opens["000010"] * 0.92), f"A 손절가 {p['stop']}")
    alloc = 100_000_000 / 10
    check(p["qty"] * opens["000010"] <= alloc, f"A 투입금 {p['qty']*opens['000010']:,.0f} ≤ 종목당 {alloc:,.0f}")
    tr = pd.read_csv(common.TRADES_FILE)
    check(len(tr) == 1 and "손절" in tr.iloc[0]["사유"], f"매매기록 {tr.iloc[0].to_dict()}")

    print("[5] 같은 신호로 다음날 다시 사지 않음")
    t2 = trader_core.Trader(b)
    run_day(t2, b, dt.date(2026, 9, 29), opens)
    check(not b.queue and set(b.hold) == {"000010"}, "추가 주문 없음")

    print("[6] 20일선 이탈 → 스캐너가 매도대상 지정 → 다음날 매도")
    d2 = pd.Timestamp("2026-09-29")
    a = prices["000010"]
    drop = a.iloc[[-1]].copy() * 0.85
    drop.index = [d2]
    prices2 = {k: v for k, v in prices.items()}
    prices2["000010"] = pd.concat([a, drop])
    mk2 = {"KOSPI": (True, d2), "KOSDAQ": (True, d2)}
    sig = scan.run(universe=uni, prices=prices2, market=mk2, now=dt.datetime(2026, 9, 29, 16, 0))
    check([s["code"] for s in sig["sells"]] == ["000010"], f"매도대상 {sig['sells']}")
    t3 = trader_core.Trader(b)
    run_day(t3, b, dt.date(2026, 9, 30), {"000010": 9999.0})
    check(not b.hold and not common.load_state()["positions"], "전량 매도, 상태 비움")
    tr = pd.read_csv(common.TRADES_FILE)
    check(len(tr) == 2 and tr.iloc[1]["매도가"] == 9999 and "20일선" in tr.iloc[1]["사유"], "매도 기록")

    print("[7] 휴장일: 주문 되돌리고 다음 거래일에 신호 재사용")
    os.remove(os.path.join(TMP, "signals_20260929.json"))   # 9/25 신호를 최신으로
    scan.run(universe=uni, prices=prices, market=mk, now=dt.datetime.combine(d0, dt.time(16, 0)))
    st = common.load_state(); st["last_signal_used"] = ""; common.save_state(st)
    b = FakeBroker(open_day=False)
    t4 = trader_core.Trader(b)
    run_day(t4, b, dt.date(2026, 9, 28), {})
    st = common.load_state()
    check(not st["positions"] and st["last_signal_used"] == "", "포지션 없음, 신호 미사용 처리")
    b.open_day, b.queue, b.quit_called = True, [], False
    t5 = trader_core.Trader(b)
    run_day(t5, b, dt.date(2026, 9, 29), opens)
    check(set(b.hold) == {"000010", "000020"}, f"다음 거래일 매수 {list(b.hold)}")

    print("[8] 주말엔 바로 종료")
    b = FakeBroker()
    t6 = trader_core.Trader(b)
    t6.tick(dt.datetime(2026, 10, 3, 8, 20))
    check(b.quit_called, "토요일 종료")

    shutil.rmtree(TMP)
    print("\n모든 테스트 통과")


if __name__ == "__main__":
    main()
