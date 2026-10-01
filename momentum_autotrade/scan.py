"""
[1단계] 장 마감 후 스캐너 — 64비트 파이썬에서 실행

하는 일
1) 코스피·코스닥 전 종목 일봉을 받아서 52주 신고가 모멘텀 조건 검사
2) 시장 필터(지수 상승추세) 확인
3) 봇이 보유 중인 종목 중 매도할 것(20일선 이탈 / 60거래일 경과) 확인
4) 결과를 data/signals_날짜.json 으로 저장 → 다음날 아침 trader.py가 읽어서 주문

실행:  python scan.py            (전 종목, 5~10분)
       python scan.py --limit 100 (100종목만, 빠른 테스트)
권장:  매일 16:00 이후 (장 마감 데이터가 확정된 뒤)
"""
import argparse
import datetime as dt
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

import config
from common import load_state, log, save_signals

EOK = 100_000_000
HISTORY_DAYS = 450   # 250거래일 + 여유


# ───────────────── 데이터 ─────────────────
def load_universe():
    import FinanceDataReader as fdr
    frames = []
    try:
        for m in ["KOSPI", "KOSDAQ"]:
            df = fdr.StockListing(m).rename(columns={"Symbol": "Code"})
            frames.append(pd.DataFrame({"Code": df["Code"], "Name": df["Name"], "Market": m}))
        uni = pd.concat(frames, ignore_index=True)
    except Exception as e:
        log(f"KRX 종목목록 실패({e}) → 네이버에서 가져옴")
        uni = _universe_naver()
    uni["Code"] = uni["Code"].astype(str).str.zfill(6)
    bad = r"스팩|ETN|KODEX|TIGER|KBSTAR|ARIRANG|HANARO|KOSEF|SOL |ACE |RISE |PLUS |리츠"
    uni = uni[uni["Code"].str.match(r"^\d{5}0$") & ~uni["Name"].str.contains(bad, regex=True)]
    return uni.drop_duplicates("Code").reset_index(drop=True)


def _universe_naver():
    import requests
    rows = []
    for sosok, market in [(0, "KOSPI"), (1, "KOSDAQ")]:
        for page in range(1, 60):
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
            html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).content.decode("euc-kr", "ignore")
            found = re.findall(r'<a href="/item/main\.naver\?code=(\d{6})" class="tltle">([^<]+)</a>', html)
            if not found:
                break
            rows += [(c, n.strip(), market) for c, n in found]
            time.sleep(0.2)
    return pd.DataFrame(rows, columns=["Code", "Name", "Market"])


def fetch(code, start):
    import FinanceDataReader as fdr
    for attempt in range(3):
        try:
            df = fdr.DataReader(code, start)
            df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            return code, df[(df["Volume"] > 0) & (df["Open"] > 0)]
        except Exception:
            time.sleep(1 + attempt)
    return code, None


def drop_unfinished_bar(df, now=None):
    """장중에 실행하면 오늘 봉이 미완성 → 제외"""
    now = now or dt.datetime.now()
    if len(df) and df.index[-1].date() == now.date() and now.time() < dt.time(15, 40):
        return df.iloc[:-1]
    return df


# ───────────────── 전략 ─────────────────
def buy_signal(df):
    """마지막 봉 기준 매수 조건 충족 여부 + 정렬용 거래대금"""
    if len(df) < 250:
        return False, 0
    h, c, v = df["High"], df["Close"], df["Volume"]
    m20, m60, m120 = (c.rolling(n).mean().iloc[-1] for n in (20, 60, 120))
    last = c.iloc[-1]
    amt20 = (c * v).rolling(20).mean().iloc[-1]
    ok = (h.iloc[-250:].max() * config.NEAR_HIGH <= last
          and m20 > m60 > m120 and last > m20
          and last >= config.MIN_PRICE and amt20 >= config.MIN_AMT_EOK * EOK)
    return bool(ok), float(last * v.iloc[-1])


def sell_signal(df, pos):
    """보유 종목 매도 여부: 종가 20일선 이탈 또는 최대 보유일 경과"""
    c = df["Close"]
    if len(c) < config.EXIT_MA:
        return None
    if c.iloc[-1] < c.rolling(config.EXIT_MA).mean().iloc[-1]:
        return f"{config.EXIT_MA}일선 이탈"
    held = int((df.index >= pd.Timestamp(pos["entry_date"])).sum())
    if held >= config.MAX_HOLD_DAYS:
        return f"{config.MAX_HOLD_DAYS}거래일 경과"
    return None


def market_ok(idx_close):
    m20 = idx_close.rolling(20).mean()
    m60 = idx_close.rolling(60).mean()
    return bool(idx_close.iloc[-1] > m20.iloc[-1] and m20.iloc[-1] > m60.iloc[-1])


def _index_naver(symbol, count=300):
    """네이버 지수 일봉 (종목 시세와 같은 출처라 날짜가 어긋나지 않음)"""
    import requests
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}"
           f"&timeframe=day&count={count}&requestType=0")
    xml = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).text
    rows = re.findall(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', xml)
    if not rows:
        raise RuntimeError("지수 데이터 없음")
    s = pd.Series([float(c) for _, c in rows], index=pd.to_datetime([d for d, _ in rows]))
    return s.sort_index()


def load_market(start, now=None):
    out = {}
    for m, naver_sym, fdr_sym in [("KOSPI", "KOSPI", "KS11"), ("KOSDAQ", "KOSDAQ", "KQ11")]:
        try:
            idx = _index_naver(naver_sym)
        except Exception as e:
            log(f"네이버 {m} 지수 실패({e}) → FinanceDataReader 사용")
            import FinanceDataReader as fdr
            idx = fdr.DataReader(fdr_sym, start)["Close"]
        idx = drop_unfinished_bar(idx.to_frame("Close"), now)["Close"]
        out[m] = (market_ok(idx), idx.index[-1])
    return out


# ───────────────── 실행 ─────────────────
def run(limit=0, universe=None, prices=None, market=None, now=None):
    now = now or dt.datetime.now()
    start = (now - dt.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    state = load_state()
    held = state["positions"]

    uni = universe if universe is not None else load_universe()
    if limit:
        uni = pd.concat([uni.head(limit), uni[uni["Code"].isin(held)]]).drop_duplicates("Code")
    names = dict(zip(uni["Code"], uni["Name"]))
    mkts = dict(zip(uni["Code"], uni["Market"]))
    for code, p in held.items():          # 목록에서 빠진 보유종목도 검사
        names.setdefault(code, p.get("name", ""))

    market = market or load_market(start, now)
    for m, (ok, d) in market.items():
        log(f"시장필터 {m}: {'통과(신규매수 가능)' if ok else '미통과(신규매수 중단)'}  지수 기준일 {d:%Y-%m-%d}")

    if prices is None:
        prices, t0 = {}, time.time()
        codes = list(names)
        with ThreadPoolExecutor(8) as ex:
            futs = [ex.submit(fetch, c, start) for c in codes]
            for i, f in enumerate(as_completed(futs), 1):
                code, df = f.result()
                if df is not None:
                    prices[code] = df
                if i % 300 == 0:
                    log(f"시세 {i}/{len(codes)} ({time.time()-t0:.0f}초)")

    # 기준일 = 종목 데이터에서 가장 많이 나온 마지막 날짜 (= 직전 거래일)
    prices = {c: drop_unfinished_bar(df, now) for c, df in prices.items()}
    lasts = pd.Series([df.index[-1] for df in prices.values() if len(df)])
    if lasts.empty:
        log("시세 데이터를 하나도 못 받음 → 인터넷 연결 확인")
        return None
    data_date = lasts.mode().iloc[0]
    for m, (ok, d) in market.items():
        if d != data_date:
            log(f"경고: {m} 지수 날짜({d:%Y-%m-%d})가 종목 기준일({data_date:%Y-%m-%d})과 다름 → 시장필터 미통과로 처리")
            market[m] = (False, d)
    log(f"기준일 {data_date:%Y-%m-%d}")

    buys, sells = [], []
    for code, df in prices.items():
        if len(df) == 0 or df.index[-1] != data_date:
            continue                        # 거래정지 등으로 오늘 데이터 없음
        if code in held:
            if held[code].get("status") == "보유":
                why = sell_signal(df, held[code])
                if why:
                    sells.append({"code": code, "name": names.get(code, ""), "reason": why,
                                  "close": float(df["Close"].iloc[-1])})
            continue
        ok, amt = buy_signal(df)
        if ok and (not config.USE_MARKET_FILTER or market.get(mkts.get(code), (False,))[0]):
            buys.append({"code": code, "name": names.get(code, ""), "market": mkts.get(code),
                         "close": float(df["Close"].iloc[-1]), "amount_eok": round(amt / EOK, 1)})

    # 보유 중인데 오늘 데이터가 없는 종목(거래정지 등) 알림
    for code, p in held.items():
        if code not in prices:
            log(f"주의: 보유종목 {code} {p.get('name','')} 시세를 못 받음 (거래정지/상장폐지 확인 필요)")

    buys.sort(key=lambda x: -x["amount_eok"])
    sig = {"data_date": f"{data_date:%Y-%m-%d}", "created": f"{now:%Y-%m-%d %H:%M}",
           "market_ok": {m: ok for m, (ok, _) in market.items()},
           "buys": buys[:30], "sells": sells}
    path = save_signals(sig)
    log(f"매수 후보 {len(buys)}개 (상위 30개 저장), 매도 대상 {len(sells)}개 → {path}")
    for b in buys[:10]:
        log(f"  매수후보 {b['code']} {b['name']} 종가 {b['close']:,.0f} 거래대금 {b['amount_eok']}억")
    for s in sells:
        log(f"  매도대상 {s['code']} {s['name']} ({s['reason']})")
    return sig


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    run(ap.parse_args().limit)
