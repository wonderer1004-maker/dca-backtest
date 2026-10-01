"""
키움 OpenAPI+ 연결부 (윈도우 + 32비트 파이썬 + PyQt5 필요)
"""
import time

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer

from common import log


def num(s):
    s = (s or "").strip().replace(",", "")
    try:
        return abs(int(float(s))) if s else 0
    except ValueError:
        return 0


class KiwoomBroker:
    def __init__(self, app, account_no=""):
        self.app = app
        self.k = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.k.OnEventConnect.connect(self._on_login)
        self.k.OnReceiveTrData.connect(self._on_tr)
        self.k.OnReceiveRealData.connect(self._on_real)
        self.k.OnReceiveChejanData.connect(self._on_chejan)
        self.k.OnReceiveMsg.connect(lambda scr, rq, tr, msg: log(f"[키움] {rq}: {msg}"))
        self.on_price = lambda code, price: None
        self.on_fill = lambda code, side, price, qty: None
        self._loop = None
        self._tr_result = None
        self._last_tr = 0
        self.account = account_no
        self.login()

    # ── 공통 ──
    def call(self, fn, *args):
        return self.k.dynamicCall(fn, *args)

    def _wait(self, sec):
        self._loop = QEventLoop()
        QTimer.singleShot(int(sec * 1000), self._loop.quit)
        self._loop.exec_()
        self._loop = None

    def _done(self):
        if self._loop:
            self._loop.quit()

    # ── 로그인 ──
    def login(self):
        self._login_err = None
        self.call("CommConnect()")
        for _ in range(120):                       # 최대 2분 대기 (자동로그인 권장)
            if self._login_err is not None:
                break
            self._wait(1)
        if self._login_err != 0:
            raise RuntimeError(f"로그인 실패 (코드 {self._login_err})")
        accs = [a for a in self.call("GetLoginInfo(QString)", "ACCNO").split(";") if a]
        if self.account and self.account not in accs:
            raise RuntimeError(f"계좌 {self.account} 없음. 보유 계좌: {accs}")
        self.account = self.account or accs[0]
        log("로그인 성공")

    def _on_login(self, err):
        self._login_err = err
        self._done()

    def is_mock(self):
        return self.call("GetLoginInfo(QString)", "GetServerGubun") == "1"

    # ── TR 조회 ──
    def _tr(self, trcode, inputs, multi=None, single=None, rqname=None):
        """조회 요청 후 응답까지 대기. 연속조회(다음 페이지) 자동 처리."""
        rqname = rqname or trcode
        rows, head, prev = [], {}, 0
        while True:
            gap = time.time() - self._last_tr
            if gap < 0.3:                          # 초당 조회 제한
                time.sleep(0.3 - gap)
            for k, v in inputs.items():
                self.call("SetInputValue(QString,QString)", k, v)
            self._tr_result = None
            ret = self.call("CommRqData(QString,QString,int,QString)", rqname, trcode, prev, "9000")
            self._last_tr = time.time()
            if ret != 0:
                raise RuntimeError(f"{trcode} 조회 요청 실패 (코드 {ret})")
            for _ in range(100):
                if self._tr_result is not None:
                    break
                self._wait(0.1)
            if self._tr_result is None:
                raise RuntimeError(f"{trcode} 응답 없음")
            nxt = self._tr_result
            if single and not head:
                head = {f: self.call("GetCommData(QString,QString,int,QString)", trcode, rqname, 0, f).strip()
                        for f in single}
            if multi:
                cnt = self.call("GetRepeatCnt(QString,QString)", trcode, rqname)
                for i in range(cnt):
                    rows.append({f: self.call("GetCommData(QString,QString,int,QString)", trcode, rqname, i, f).strip()
                                 for f in multi})
            if not (multi and nxt == "2"):
                return head, rows
            prev = 2

    def _on_tr(self, scr, rqname, trcode, record, prev_next, *args):
        self._tr_result = prev_next or "0"
        self._done()

    def balance(self):
        """(추정자산, D+2 예수금, {코드: {qty, avg, cur, name}})"""
        acc = {"계좌번호": self.account, "비밀번호": "", "비밀번호입력매체구분": "00"}
        head, rows = self._tr("opw00018", {**acc, "조회구분": "2"},
                              multi=["종목번호", "종목명", "보유수량", "매입가", "현재가"],
                              single=["추정예탁자산"])
        cash_head, _ = self._tr("opw00001", {**acc, "조회구분": "2"}, single=["d+2추정예수금"])
        holdings = {}
        for r in rows:
            code = r["종목번호"].lstrip("A")[-6:]
            if num(r["보유수량"]) > 0:
                holdings[code] = {"qty": num(r["보유수량"]), "avg": num(r["매입가"]),
                                  "cur": num(r["현재가"]), "name": r["종목명"]}
        return num(head.get("추정예탁자산")), num(cash_head.get("d+2추정예수금")), holdings

    def market_open(self):
        """삼성전자 오늘 거래량이 0이면 휴장일"""
        head, _ = self._tr("opt10001", {"종목코드": "005930"}, single=["거래량"])
        return num(head.get("거래량")) > 0

    # ── 주문 ──
    def order(self, side, code, qty):
        typ = 1 if side == "buy" else 2            # 1 신규매수, 2 신규매도
        ret = self.call("SendOrder(QString,QString,QString,int,QString,int,int,QString,QString)",
                        ["auto_" + side, "8000", self.account, typ, code, int(qty), 0, "03", ""])  # 03 = 시장가
        time.sleep(0.25)                           # 초당 주문 제한
        return ret == 0

    def _on_chejan(self, gubun, item_cnt, fid_list):
        if gubun != "0":                           # 0 = 주문접수/체결
            return
        get = lambda fid: self.call("GetChejanData(int)", fid).strip()
        qty = num(get(911))                        # 이번 체결량
        if qty <= 0:
            return
        code = get(9001).lstrip("A")[-6:]
        side = "sell" if get(907) == "1" else "buy"  # 907: 1 매도, 2 매수
        self.on_fill(code, side, num(get(910)), qty)

    # ── 실시간 시세 ──
    def subscribe(self, codes):
        self.call("SetRealReg(QString,QString,QString,QString)", "5000", ";".join(codes), "10", "0")

    def _on_real(self, code, real_type, data):
        if real_type == "주식체결":
            price = num(self.call("GetCommRealData(QString,int)", code, 10))
            if price:
                self.on_price(code, price)

    def quit(self):
        try:
            self.call("SetRealRemove(QString,QString)", "ALL", "ALL")
        finally:
            QTimer.singleShot(1000, self.app.quit)
