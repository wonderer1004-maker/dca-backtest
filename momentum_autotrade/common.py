"""
스캐너와 주문 프로그램이 같이 쓰는 파일 입출력 (pandas 없이 동작)
"""
import csv
import datetime as dt
import glob
import json
import os

import config

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, config.DATA_DIR)
LOGS = os.path.join(HERE, config.LOG_DIR)
STATE_FILE = os.path.join(DATA, "state.json")
TRADES_FILE = os.path.join(DATA, "trades.csv")

os.makedirs(DATA, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)


def log(msg):
    now = dt.datetime.now()
    line = f"[{now:%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOGS, f"{now:%Y%m%d}.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)   # 쓰는 도중 꺼져도 파일이 깨지지 않게


def load_state():
    """봇이 산 종목만 관리. 직접 산 종목은 건드리지 않음.
    positions: {코드: {name, entry_date, entry_price, qty, stop, status}}
      status: "주문중" (아침 매수 주문 후 체결 확인 전) / "보유" / "매도주문"
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"positions": {}, "last_run": ""}


def save_state(state):
    _write_json(STATE_FILE, state)


def save_signals(sig):
    path = os.path.join(DATA, f"signals_{sig['data_date'].replace('-', '')}.json")
    _write_json(path, sig)
    return path


def latest_signals():
    files = sorted(glob.glob(os.path.join(DATA, "signals_*.json")))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


def record_trade(code, name, entry_date, entry_price, exit_date, exit_price, qty, reason):
    new = not os.path.exists(TRADES_FILE)
    ret = (exit_price / entry_price - 1) * 100 if entry_price else 0
    with open(TRADES_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["종목코드", "종목명", "매수일", "매수가", "매도일", "매도가", "수량", "수익률%", "사유"])
        w.writerow([code, name, entry_date, entry_price, exit_date, exit_price, qty, round(ret, 2), reason])
