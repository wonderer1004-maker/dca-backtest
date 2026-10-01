"""
키움 조건검색식 백테스트 (일봉 기준)

- 대상: 코스피 + 코스닥 보통주 (우선주, 스팩, ETF/ETN 제외)
- 데이터: FinanceDataReader (네이버 일봉)
- 매수: 신호 다음날 시가
- 매도: 익절 / 손절 / 최대 보유일 (전략 유형별로 다름)
- 같은 종목은 보유 중이면 새 신호 무시 (중복 매수 없음)
- 비용: 왕복 0.25% (수수료 + 세금 + 슬리피지 대략값)

실행:
    python backtest.py            # 전체 종목
    python backtest.py --limit 50 # 50종목만 (빠른 테스트)
    python backtest.py --demo     # 인터넷 없이 가짜 데이터로 코드 점검
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

DATA_START = "2016-01-01"   # 240일선, 주봉 140봉 계산용 여유
TEST_START = "2020-01-01"   # 이 날짜 이후 신호만 성과에 반영
COST = 0.0025
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

EXIT_DAY = dict(tp=0.10, sl=-0.05, hold=5)     # 단기형
EXIT_SWING = dict(tp=0.15, sl=-0.07, hold=20)  # 스윙형

EOK = 100_000_000  # 1억원


# ───────────────────────── 종목 목록 ─────────────────────────
def load_universe():
    """코스피+코스닥 종목코드, 이름, 시장, 시가총액(억원)"""
    try:
        import FinanceDataReader as fdr
        frames = []
        for m in ["KOSPI", "KOSDAQ"]:
            df = fdr.StockListing(m)
            df = df.rename(columns={"Symbol": "Code"})
            df["Market"] = m
            cap = df["Marcap"] / EOK if "Marcap" in df.columns else np.nan
            frames.append(pd.DataFrame({"Code": df["Code"], "Name": df["Name"],
                                        "Market": m, "MarcapEok": cap}))
        uni = pd.concat(frames, ignore_index=True)
        print(f"[목록] KRX에서 {len(uni)}종목", flush=True)
    except Exception as e:
        print(f"[목록] KRX 실패({e}) → 네이버 시가총액 페이지 사용", flush=True)
        uni = load_universe_naver()
    uni["Code"] = uni["Code"].astype(str).str.zfill(6)
    bad_name = r"스팩|ETN|KODEX|TIGER|KBSTAR|ARIRANG|HANARO|KOSEF|SOL |ACE |RISE |PLUS |리츠"
    uni = uni[uni["Code"].str.match(r"^\d{5}0$")]             # 보통주만 (우선주 코드 끝 5,7,9,K 제외)
    uni = uni[~uni["Name"].str.contains(bad_name, regex=True)]
    return uni.drop_duplicates("Code").reset_index(drop=True)


def load_universe_naver():
    import requests
    rows = []
    for sosok, market in [(0, "KOSPI"), (1, "KOSDAQ")]:
        for page in range(1, 60):
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
            html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).content.decode("euc-kr", "ignore")
            found = re.findall(r'<a href="/item/main\.naver\?code=(\d{6})" class="tltle">([^<]+)</a>(.*?)</tr>', html, re.S)
            if not found:
                break
            for code, name, rest in found:
                nums = re.findall(r'<td class="number">\s*([\d,\.]+)', rest)
                # 현재가, 등락률, 액면가, 시가총액(억) ... 순서
                cap = float(nums[3].replace(",", "")) if len(nums) > 3 else np.nan
                rows.append((code, name.strip(), market, cap))
            time.sleep(0.2)
    print(f"[목록] 네이버에서 {len(rows)}종목", flush=True)
    return pd.DataFrame(rows, columns=["Code", "Name", "Market", "MarcapEok"])


# ───────────────────────── 시세 ─────────────────────────
def fetch_one(code):
    import FinanceDataReader as fdr
    for attempt in range(3):
        try:
            df = fdr.DataReader(code, DATA_START)
            if df is None or len(df) == 0:
                return code, None
            df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            df = df[(df["Volume"] > 0) & (df["Open"] > 0)]   # 거래정지일 제거
            return code, df
        except Exception:
            time.sleep(1 + attempt)
    return code, None


def load_prices(codes, workers=8):
    data = {}
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(fetch_one, c) for c in codes]
        for i, f in enumerate(as_completed(futs), 1):
            code, df = f.result()
            if df is not None and len(df) > 300:
                data[code] = df
            if i % 200 == 0:
                print(f"[시세] {i}/{len(codes)} ({time.time()-t0:.0f}초)", flush=True)
    print(f"[시세] 사용 가능 {len(data)}종목", flush=True)
    return data


def load_bps_history():
    """pykrx로 매월 말 전 종목 BPS(주당순자산) → PBR = 종가 / BPS. 실패하면 None (⑪ 건너뜀)"""
    try:
        from pykrx import stock
        snaps, fails = {}, 0
        for dt in pd.date_range(pd.Timestamp(TEST_START) - pd.DateOffset(months=1),
                                pd.Timestamp.today(), freq="BME"):
            try:
                f = stock.get_market_fundamental(dt.strftime("%Y%m%d"), market="ALL")
                if f is not None and len(f) and "BPS" in f.columns:
                    snaps[dt] = f["BPS"].replace(0, np.nan)
                else:
                    fails += 1
            except Exception:
                fails += 1
            if not snaps and fails >= 3:
                print("[PBR] pykrx 응답 없음 → ⑪ 제외", flush=True)
                return None
            time.sleep(0.3)
        if not snaps:
            return None
        bps = pd.DataFrame(snaps).T.sort_index()      # 행: 날짜, 열: 종목코드
        print(f"[PBR] BPS 스냅샷 {len(bps)}개월", flush=True)
        return bps
    except Exception as e:
        print(f"[PBR] pykrx 실패: {e}", flush=True)
        return None


# ───────────────────────── 지표 도우미 ─────────────────────────
def ma(s, n):
    return s.rolling(n).mean()


def within(cond, n):
    """최근 n봉 이내(오늘 포함) 1회 이상"""
    return cond.astype(float).rolling(n, min_periods=1).max() == 1


def new_high(h, n):
    return h >= h.rolling(n).max()


def rising(s, times):
    """이평 상승추세 유지 times회 이상 (연속 상승)"""
    up = s > s.shift(1)
    return up.astype(float).rolling(times).sum() == times


def weekly_to_daily(series_w, daily_index):
    """주봉 지표를 '직전 완성 주' 값으로 일봉에 붙임 (미래 정보 방지)"""
    s = series_w.astype(float).shift(1)
    return s.reindex(daily_index, method="ffill").fillna(0) == 1


# ───────────────────────── 조건식 ─────────────────────────
def s1_daesise(d, meta):
    """① 대시세 초입 (하승훈): A and B and C"""
    c, v = d["Close"], d["Volume"]
    m240 = ma(c, 240)
    A = (c > m240) & (c.shift(1) <= m240.shift(1))
    B = v >= v.shift(1) * 5          # 전일 동시간대 → 일봉 전일 거래량으로 근사
    C = c / c.shift(1) - 1 >= 0.05
    return A & B & C


def s1b_daesise_nh(d, meta):
    """①+D 대시세 초입 + 60일 신고가"""
    return s1_daesise(d, meta) & new_high(d["High"], 60)


def s2_ichimoku(d, meta):
    """② 일목균형표 눌림 (하승훈)"""
    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span1 = ((tenkan + kijun) / 2).shift(25)
    span2 = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(25)
    amt = c * v
    A = (c > tenkan).shift(2, fill_value=False)
    B = (c / kijun - 1).abs() <= 0.01
    C = (l / kijun - 1).abs() <= 0.01
    D = (o / kijun - 1).abs() <= 0.01
    E = c > span1
    F = c > span2
    G = o < c
    O = rising(ma(c, 120), 2)
    P = rising(ma(c, 240), 2)
    Q = within(new_high(h, 120), 20)
    R = within(amt >= 500 * EOK, 20)
    S = within(v >= v.shift(1) * 5, 20)
    return A & (B | C | D) & E & F & G & O & P & Q & R & S


def s3_pullback20(d, meta):
    """③ 20일선 눌림목 (시윤주식)"""
    h, l, c, v = d["High"], d["Low"], d["Close"], d["Volume"]
    m5, m20, m60, m200 = ma(c, 5), ma(c, 20), ma(c, 60), ma(c, 200)
    A = c >= 1000
    H = v >= 200_000
    C = c / c.shift(1) - 1 <= 0.05
    hh = h.shift(5).rolling(20).max()
    ll = l.shift(5).rolling(20).min()
    D = (hh / ll - 1) >= 0.15
    E = (m5 > m20) & (m20 > m60)
    disp = c / m20 * 100
    F = (disp >= 100) & (disp <= 105)
    G = c > m5
    I = c > m200
    return A & H & C & D & E & F & G & I


def s4_converge(d, meta):
    """④ 이평선 수렴 (시윤주식, 주봉+일봉)"""
    h, c, v = d["High"], d["Close"], d["Volume"]
    w = d.resample("W-FRI").agg({"High": "max", "Close": "last"}).dropna()
    wnh = new_high(w["High"], 100)
    wC = within(wnh, 30).shift(10, fill_value=False)
    wD = (ma(w["Close"], 10) > ma(w["Close"], 20)) & (ma(w["Close"], 20) > ma(w["Close"], 60))
    Cw = weekly_to_daily(wC, d.index)
    Dw = weekly_to_daily(wD, d.index)
    m20, m60, m120 = ma(c, 20), ma(c, 60), ma(c, 120)
    A = c >= 1000
    B = v >= 100_000
    E = (m20 / m60 - 1).abs() <= 0.05
    F = (m20 / m120 - 1).abs() <= 0.05
    G = (m60 / m120 - 1).abs() <= 0.05
    H = (c / m20 - 1).abs() <= 0.05
    return A & B & Cw & Dw & E & F & G & H


def s5_newhigh4(d, meta):
    """⑤ 신고가 돌파 '4%' (창원개미) — 시가총액은 현재값 기준(근사)"""
    h, c, v = d["High"], d["Close"], d["Volume"]
    if meta.get("Market") != "KOSDAQ":
        return pd.Series(False, index=d.index)
    cap_ok = (meta.get("MarcapEok") or 0) >= 700
    A = h >= h.rolling(15).max() * 0.95
    B = v >= 200_000
    E = (c >= 800) & (c <= 150_000)
    return A & B & E & cap_ok


def s6_leader(d, meta):
    """⑥ 데이트레이딩 주도주 (키움)"""
    h, c, v = d["High"], d["Close"], d["Volume"]
    A = within(new_high(h, 60), 5)
    B = within(c * v >= 800 * EOK, 5)
    C = h >= h.rolling(20).max() * 0.90
    D = within(c / c.shift(1) - 1 >= 0.07, 5)
    return A & B & C & D


AMT_ASSUMED = 100  # ⑧⑨ 거래대금 기준(억) — 캡처에서 잘려 가정값 사용


def s8_react(d, meta):
    """⑧ 스윙 대응형 (키움)"""
    c, v = d["Close"], d["Volume"]
    A = within(c / c.shift(1) - 1 >= 0.10, 10)
    B = within(c * v >= AMT_ASSUMED * EOK, 10)
    disp = c / ma(c, 20) * 100
    C = (disp >= 97) & (disp <= 103)
    return A & B & C


def s9_predict(d, meta):
    """⑨ 스윙 예측형 – 매집 흔적 (키움)"""
    c, v = d["Close"], d["Volume"]
    A = within(v >= v.shift(1) * 10, 10)
    B = within(c * v >= AMT_ASSUMED * EOK, 10)
    E = v <= v.rolling(10).max() * 0.10
    return A & B & E


def s11_lowpbr(d, meta):
    """⑪ 저PBR 급등 — meta['PBR'] 필요"""
    bps = meta.get("BPS")
    if bps is None:
        return pd.Series(False, index=d.index)
    c = d["Close"]
    # 월말 BPS는 다음 거래일부터 사용 (미래 정보 방지)
    bps_d = bps.shift(1, freq="D").reindex(d.index.union(bps.index)).ffill().reindex(d.index)
    pbr = c / bps_d
    C = (pbr > 0) & (pbr <= 0.3)
    D = c > ma(c, 200)
    E = c / c.shift(1) - 1 >= 0.05
    return C & D & E


def base_every10(d, meta):
    """비교용 기준선: 조건 없이 10거래일마다 매수"""
    s = pd.Series(False, index=d.index)
    s.iloc[::10] = True
    return s


STRATEGIES = [
    ("기준선(조건없이 매수) 단기", base_every10, EXIT_DAY),
    ("기준선(조건없이 매수) 스윙", base_every10, EXIT_SWING),
    ("①  대시세 초입", s1_daesise, EXIT_DAY),
    ("①+ 대시세 초입+60일신고가", s1b_daesise_nh, EXIT_DAY),
    ("②  일목균형표 눌림", s2_ichimoku, EXIT_SWING),
    ("③  20일선 눌림목", s3_pullback20, EXIT_SWING),
    ("④  이평선 수렴", s4_converge, EXIT_SWING),
    ("⑤  신고가 돌파 4%", s5_newhigh4, EXIT_DAY),
    ("⑥  데이 주도주", s6_leader, EXIT_DAY),
    ("⑧  스윙 대응형", s8_react, EXIT_SWING),
    ("⑨  스윙 예측형", s9_predict, EXIT_SWING),
    ("⑪  저PBR 급등", s11_lowpbr, EXIT_SWING),
]


# ───────────────────────── 매매 시뮬레이션 ─────────────────────────
def simulate(d, sig, tp, sl, hold):
    o, h, l, c = (d[k].to_numpy() for k in ["Open", "High", "Low", "Close"])
    idx = d.index
    sig = sig.fillna(False).to_numpy()
    start = idx.searchsorted(pd.Timestamp(TEST_START))
    trades, i, n = [], start, len(d)
    while i < n - 1:
        if not sig[i]:
            i += 1
            continue
        e = i + 1
        entry = o[e]
        stop, take = entry * (1 + sl), entry * (1 + tp)
        exit_p, reason, j = None, "기간만료", e
        for j in range(e, min(e + hold, n)):
            if j > e and o[j] <= stop:            # 갭하락 손절
                exit_p, reason = o[j], "손절"; break
            if j > e and o[j] >= take:            # 갭상승 익절
                exit_p, reason = o[j], "익절"; break
            if l[j] <= stop:                      # 같은 날 둘 다 닿으면 손절로 (보수적)
                exit_p, reason = stop, "손절"; break
            if h[j] >= take:
                exit_p, reason = take, "익절"; break
        if exit_p is None:
            j = min(e + hold - 1, n - 1)
            exit_p = c[j]
        trades.append((idx[i], idx[e], idx[j], entry, exit_p, exit_p / entry - 1 - COST, reason))
        i = j + 1                                  # 보유 중 신호 무시
    return trades


# ───────────────────────── 요약 ─────────────────────────
def summarize(name, t):
    if t.empty:
        return {"전략": name, "거래수": 0}
    r = t["수익률"]
    win, lose = r[r > 0], r[r <= 0]
    pf = win.sum() / -lose.sum() if lose.sum() < 0 else np.inf
    return {
        "전략": name,
        "거래수": len(t),
        "승률%": round(len(win) / len(r) * 100, 1),
        "평균%": round(r.mean() * 100, 2),
        "중앙값%": round(r.median() * 100, 2),
        "평균이익%": round(win.mean() * 100, 2) if len(win) else 0,
        "평균손실%": round(lose.mean() * 100, 2) if len(lose) else 0,
        "손익비(PF)": round(pf, 2),
        "평균보유(달력일)": round(t["보유일"].mean(), 1),
    }


def by_year(t):
    if t.empty:
        return pd.DataFrame()
    g = t.groupby(t["신호일"].dt.year)["수익률"]
    return pd.DataFrame({"거래수": g.size(), "승률%": (g.apply(lambda x: (x > 0).mean() * 100)).round(1),
                         "평균%": (g.mean() * 100).round(2)})


def make_demo(n_codes=40):
    rng = np.random.default_rng(0)
    idx = pd.bdate_range(DATA_START, "2026-09-30")
    data, rows = {}, []
    for k in range(n_codes):
        ret = rng.normal(0.0004, 0.03, len(idx))
        ret[rng.random(len(idx)) < 0.01] += 0.12        # 가끔 급등
        c = 5000 * np.exp(np.cumsum(ret))
        o = c * (1 + rng.normal(0, 0.01, len(idx)))
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.015, len(idx))))
        l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.015, len(idx))))
        v = rng.lognormal(12.5, 0.8, len(idx))
        v[rng.random(len(idx)) < 0.01] *= 15
        code = f"{100000 + k * 10:06d}"
        data[code] = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}, index=idx)
        rows.append((code, f"DEMO{k}", "KOSDAQ" if k % 2 else "KOSPI", 1000.0))
    return pd.DataFrame(rows, columns=["Code", "Name", "Market", "MarcapEok"]), data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    if args.demo:
        uni, prices = make_demo()
        pbr = None
    else:
        uni = load_universe()
        if args.limit:
            uni = uni.head(args.limit)
        prices = load_prices(uni["Code"].tolist())
        pbr = load_bps_history()
    meta_map = uni.set_index("Code").to_dict("index")
    if pbr is not None:
        for c in pbr.columns:
            if c in meta_map:
                meta_map[c]["BPS"] = pbr[c].dropna()
    if args.demo:   # 가짜 BPS로 ⑪ 코드 경로도 점검
        pbr = True
        for c, d in prices.items():
            meta_map[c]["BPS"] = d["Close"].resample("BME").last() * 4
    names = uni.set_index("Code")["Name"].to_dict()

    os.makedirs(OUT_DIR, exist_ok=True)
    summary, years, notes = [], {}, []
    cols = ["신호일", "매수일", "매도일", "매수가", "매도가", "수익률", "사유"]
    for name, fn, ex in STRATEGIES:
        if fn is s11_lowpbr and pbr is None:
            notes.append("⑪ 저PBR: 과거 PBR 데이터(pykrx)를 받지 못해 제외")
            continue
        allt = []
        for code, d in prices.items():
            try:
                sig = fn(d, meta_map.get(code, {}))
                for tr in simulate(d, sig, **ex):
                    allt.append((code, names.get(code, ""), *tr))
            except Exception as e:
                print(f"  {name} {code} 오류: {e}", flush=True)
        t = pd.DataFrame(allt, columns=["종목코드", "종목명"] + cols)
        if not t.empty:
            t["보유일"] = (t["매도일"] - t["매수일"]).dt.days
        s = summarize(name, t)
        s["매도규칙"] = f"+{int(ex['tp']*100)}% / {int(ex['sl']*100)}% / {ex['hold']}거래일"
        summary.append(s)
        years[name] = by_year(t)
        fname = re.sub(r"[^0-9A-Za-z가-힣]+", "_", name).strip("_")
        t.sort_values("신호일").to_csv(os.path.join(OUT_DIR, f"trades_{fname}.csv"),
                                       index=False, encoding="utf-8-sig")
        print(f"[완료] {name}: {s.get('거래수')}건", flush=True)

    sm = pd.DataFrame(summary)
    sm.to_csv(os.path.join(OUT_DIR, "summary.csv"), index=False, encoding="utf-8-sig")

    lines = ["# 조건검색식 백테스트 결과", "",
             f"- 실행일: {pd.Timestamp.now(tz='Asia/Seoul'):%Y-%m-%d %H:%M} (KST)",
             f"- 기간: {TEST_START} ~ 데이터 마지막 날 / 대상 {len(prices)}종목 {'(가짜 데이터)' if args.demo else ''}",
             "- 매수: 신호 다음날 시가 / 비용 왕복 0.25% / 보유 중 같은 종목 재매수 안 함",
             f"- ⑧⑨ 거래대금 기준은 캡처에서 잘려 {AMT_ASSUMED}억으로 가정, 거래대금 = 종가×거래량(근사)",
             "- ⑤ 시가총액은 현재 기준(과거 시총 아님), ① 거래량 조건은 '전일 동시간대' 대신 '전일 거래량'으로 근사",
             "- 기준선 = 조건 없이 10거래일마다 산 경우. 조건식이 기준선보다 좋아야 의미가 있음",
             "- 상장폐지 종목은 데이터에 없어 결과가 실제보다 좋게 나올 수 있음", ""]
    lines += [f"- {n}" for n in notes]
    lines += ["", "## 전략별 요약", "", sm.to_markdown(index=False), "", "## 연도별", ""]
    for name, y in years.items():
        if y.empty:
            continue
        lines += [f"### {name}", "", y.to_markdown(), ""]
    lines.append(f"\n소요시간 {time.time()-t0:.0f}초")
    with open(os.path.join(OUT_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
