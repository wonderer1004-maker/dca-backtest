"""
[2단계] 아침 주문 + 장중 손절 감시 — 32비트 파이썬에서 실행 (키움 OpenAPI+)

실행:  python trader.py
권장:  평일 08:20에 자동 실행 (윈도우 작업 스케줄러), 15:25에 스스로 종료
"""
import datetime as dt
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

import config
from common import log
from kiwoom_broker import KiwoomBroker
from trader_core import Trader


def main():
    app = QApplication(sys.argv)
    log("=" * 50)
    log(f"자동매매 시작 ({'실전' if config.IS_REAL else '모의투자'}{', DRY_RUN' if config.DRY_RUN else ''})")
    broker = KiwoomBroker(app, config.ACCOUNT_NO)
    trader = Trader(broker)
    timer = QTimer()
    timer.timeout.connect(lambda: trader.tick(dt.datetime.now()))
    timer.start(1000)
    app.exec_()
    log("자동매매 종료")


if __name__ == "__main__":
    main()
