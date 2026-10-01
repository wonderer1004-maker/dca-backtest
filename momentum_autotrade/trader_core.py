"""
주문 로직 (증권사와 무관한 부분) — 키움 없이도 테스트할 수 있게 분리

하루 흐름
  시작 → 잔고 동기화 → 어젯밤 신호 확인
  ORDER_TIME(08:45) → 매도 주문 먼저, 그다음 매수 주문 (장전 동시호가, 시장가)
  09:03 → 체결 확인, 보유 종목 실시간 시세 등록
  장중 → 현재가가 손절가(매수가 -8%) 이하면 즉시 시장가 매도
  5분마다 잔고 확인 / END_TIME(15:25) → 저장 후 종료
"""
import datetime as dt
import math

import config
from common import latest_signals, load_state, log, record_trade, save_state


def hm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


class Trader:
    def __init__(self, broker):
        self.b = broker
        self.phase = "init"
        self.state = load_state()
        self.pos = self.state["positions"]
        self.last_price = {}
        self.fills = {}           # code → {"buy": [금액, 수량], "sell": [금액, 수량]}
        self.next_sync = None
        self.orders_sent = False
        self.today = None
        b = self.b
        b.on_price = self.on_price
        b.on_fill = self.on_fill

    # ───────── 매 초 호출 ─────────
    def tick(self, now):
        if self.phase == "done" or getattr(self, "_busy", False):
            return                  # 조회 응답 대기 중에 타이머가 또 불러도 겹쳐 실행되지 않게
        self._busy = True
        try:
            self._tick(now)
        except Exception as e:      # 한 번의 오류로 프로그램이 죽지 않게
            log(f"오류: {e!r}")
        finally:
            self._busy = False

    def _tick(self, now):
        t = now.time()
        if self.phase == "init":
            self.today = now.strftime("%Y-%m-%d")
            if now.weekday() >= 5:
                log("주말 → 종료"); return self.finish()
            if not self.start():
                return self.finish()
            self.phase = "wait_order"
        if self.phase == "wait_order" and t >= hm(config.ORDER_TIME):
            self.place_morning_orders(late=t > hm(config.LATE_LIMIT))
            self.phase = "ordered"
        if self.phase == "ordered" and t >= dt.time(9, 3):
            if self.orders_sent and not self.b.market_open():
                log("오늘 거래가 없음(휴장일로 판단) → 신호는 다음 거래일에 사용")
                self.state["last_signal_used"] = self.state.get("prev_signal_used", "")
                for code in list(self.pos):            # 휴장일 주문은 무효 → 되돌림
                    if self.pos[code]["status"] == "주문중":
                        del self.pos[code]
                    elif self.pos[code]["status"] == "매도주문":
                        self.pos[code]["status"] = "보유"
                        self.pos[code].pop("exit_reason", None)
                save_state(self.state)
                return self.finish()
            self.sync(now)
            self.subscribe()
            self.phase = "monitor"
            self.next_sync = now + dt.timedelta(minutes=5)
        if self.phase == "monitor":
            if now >= self.next_sync:
                self.sync(now)
                self.subscribe()
                self.next_sync = now + dt.timedelta(minutes=5)
            if t >= hm(config.END_TIME):
                self.sync(now)
                log(self.summary())
                return self.finish()

    def finish(self):
        self.phase = "done"
        save_state(self.state)
        self.b.quit()

    # ───────── 시작 ─────────
    def start(self):
        mock = self.b.is_mock()
        log(f"접속 서버: {'모의투자' if mock else '실전'} / 계좌 {self.b.account}")
        if not config.IS_REAL and not mock:
            log("config.IS_REAL=False 인데 실전 서버에 로그인됨 → 안전을 위해 종료. 모의투자로 로그인하세요.")
            return False
        if config.IS_REAL and mock:
            log("config.IS_REAL=True 인데 모의투자 서버임 → 설정 확인 후 다시 실행")
            return False
        self.sync(None)
        log(self.summary())
        return True

    # ───────── 잔고 동기화 ─────────
    def sync(self, now):
        nav, cash, holdings = self.b.balance()
        self.nav, self.cash, self.holdings = nav, cash, holdings
        for code in list(self.pos):
            p = self.pos[code]
            qty = holdings.get(code, {}).get("qty", 0)
            if p["status"] == "주문중":
                if qty > 0:
                    p["status"] = "보유"
                    p["qty"] = qty
                    price = self._fill_price(code, "buy") or holdings[code]["avg"]
                    p["entry_price"] = price
                    p["stop"] = math.floor(price * (1 + config.STOP_LOSS))
                    log(f"매수 체결 {code} {p['name']} {qty}주 @ {price:,.0f} (손절가 {p['stop']:,})")
                elif now and now.time() >= dt.time(9, 30):
                    log(f"매수 미체결 {code} {p['name']} → 포지션 취소")
                    del self.pos[code]
            elif qty == 0:                      # 보유/매도주문 → 잔고에서 사라짐 = 매도 완료
                price = self._fill_price(code, "sell") or self.last_price.get(code) or p.get("entry_price", 0)
                record_trade(code, p["name"], p["entry_date"], p["entry_price"], self.today,
                             price, p["qty"], p.get("exit_reason", "잔고없음(수동매도?)"))
                log(f"매도 완료 {code} {p['name']} @ {price:,.0f} "
                    f"({(price / p['entry_price'] - 1) * 100 if p['entry_price'] else 0:+.1f}%) {p.get('exit_reason','')}")
                del self.pos[code]
            elif qty != p["qty"]:
                p["qty"] = qty                  # 부분 체결 반영
        save_state(self.state)

    def _fill_price(self, code, side):
        f = self.fills.get(code, {}).get(side)
        return round(f[0] / f[1]) if f and f[1] else None

    # ───────── 아침 주문 ─────────
    def place_morning_orders(self, late):
        sig = latest_signals()
        if not sig:
            log("신호 파일 없음 → scan.py를 먼저 실행하세요. 오늘은 손절 감시만 합니다.")
            return
        used = self.state.get("last_signal_used", "")
        if sig["data_date"] <= used:
            log(f"최신 신호({sig['data_date']})는 이미 사용함 → 오늘 신규 주문 없음 (scan.py 실행 여부 확인)")
            return
        age = (dt.date.fromisoformat(self.today) - dt.date.fromisoformat(sig["data_date"])).days
        if age > 5:
            log(f"신호가 {age}일 지났음 → 오래된 신호라 사용 안 함")
            return
        self.state["prev_signal_used"], self.state["last_signal_used"] = used, sig["data_date"]

        # 1) 매도
        n_sell = 0
        for s in sig["sells"]:
            p = self.pos.get(s["code"])
            if p and p["status"] == "보유":
                self.sell(s["code"], s["reason"])
                n_sell += 1

        # 2) 매수
        if late:
            log(f"{config.LATE_LIMIT} 이후 시작 → 오늘 신규 매수는 건너뜀")
            save_state(self.state); return
        if not any(sig["market_ok"].values()):
            log("시장필터 미통과 → 신규 매수 없음")
        free = config.SLOTS - (len(self.pos) - n_sell)
        free = min(free, config.MAX_BUY_PER_DAY)
        alloc = self.nav * config.INVEST_RATIO / config.SLOTS
        cash = self.cash
        bought = 0
        for c in sig["buys"]:
            if bought >= free:
                break
            code = c["code"]
            if code in self.pos or code in self.holdings:   # 이미 보유(직접 산 종목 포함)는 제외
                continue
            unit = c["close"] * 1.03                         # 갭상승 대비 3% 여유
            qty = int(min(alloc, cash) // unit)
            if qty <= 0:
                log(f"예수금 부족 → 매수 중단 (남은 예수금 {cash:,.0f})")
                break
            if self.order("buy", code, qty, c["name"]):
                self.pos[code] = {"name": c["name"], "entry_date": self.today, "entry_price": 0,
                                  "qty": qty, "stop": 0, "status": "주문중"}
                cash -= qty * unit
                bought += 1
        log(f"아침 주문: 매도 {n_sell}건, 매수 {bought}건 (빈 자리 {free}, 종목당 {alloc:,.0f}원)")
        save_state(self.state)

    def sell(self, code, reason):
        p = self.pos[code]
        if self.order("sell", code, p["qty"], p["name"]):
            p["status"] = "매도주문"
            p["exit_reason"] = reason

    def order(self, side, code, qty, name=""):
        word = "매수" if side == "buy" else "매도"
        if config.DRY_RUN:
            log(f"[DRY_RUN] {word} {code} {name} {qty}주 시장가 (실제 주문 안 함)")
            return True
        ok = self.b.order(side, code, qty)
        log(f"{word} 주문 {'접수' if ok else '실패'}: {code} {name} {qty}주 시장가")
        self.orders_sent = self.orders_sent or ok
        return ok

    # ───────── 실시간 ─────────
    def subscribe(self):
        codes = [c for c, p in self.pos.items() if p["status"] in ("보유", "매도주문")]
        if codes:
            self.b.subscribe(codes)

    def on_price(self, code, price):
        self.last_price[code] = price
        p = self.pos.get(code)
        if p and p["status"] == "보유" and p["stop"] and price <= p["stop"]:
            log(f"손절 발동 {code} {p['name']} 현재가 {price:,} ≤ 손절가 {p['stop']:,}")
            self.sell(code, f"손절 {config.STOP_LOSS*100:.0f}%")
            save_state(self.state)

    def on_fill(self, code, side, price, qty):
        f = self.fills.setdefault(code, {}).setdefault(side, [0, 0])
        f[0] += price * qty
        f[1] += qty

    def summary(self):
        lines = [f"계좌 추정자산 {self.nav:,.0f}원 / 예수금(D+2) {self.cash:,.0f}원 / 봇 보유 {len(self.pos)}종목"]
        for c, p in self.pos.items():
            cur = self.last_price.get(c) or self.holdings.get(c, {}).get("cur", 0)
            r = (cur / p["entry_price"] - 1) * 100 if p["entry_price"] and cur else 0
            lines.append(f"  {c} {p['name']} {p['qty']}주 매수가 {p['entry_price']:,.0f} 현재 {cur:,.0f} ({r:+.1f}%) [{p['status']}]")
        return "\n".join(lines)
