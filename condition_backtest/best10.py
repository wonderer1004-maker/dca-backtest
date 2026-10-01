"""
새로 설계한 조건검색식 10종 백테스트

설계 원칙
- 학계·실전에서 반복 확인된 패턴만 사용: 추세추종(신고가 모멘텀), 변동성 축소 후 돌파,
  상승추세 속 단기 과매도 반등, 주도주 첫 눌림 등
- 공통 필터: 주가 1,000원 이상 + 20일 평균 거래대금 20억 이상 (호가 얇은 종목 제외)
- 시장 필터: 해당 시장 지수(코스피/코스닥)가 20일선 위일 때만 매수 (하락장 회피)
- 매도: 손절가 + 추세 이탈(종가가 이평선 아래로 내려가면 다음날 시가 매도) + 최대 보유일
- 모든 조건은 키움 조건검색 지표로 입력 가능한 형태

검증
- 전반기 2020~2023 / 후반기 2024~2026.9 를 따로 계산 → 둘 다 플러스여야 신뢰
- 거래 1회 평균 + "10종목 분산 포트폴리오" 시뮬레이션(실제 계좌처럼 동시 보유 10개 한도)
"""
import argparse
import os
import time

import numpy as np
import pandas as pd

from backtest import (COST, EOK, TEST_START, load_universe, load_prices, make_demo,
                      ma, within, new_high, rising)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_best10")
SPLIT = pd.Timestamp("2024-01-01")
SLOTS = 10


# ───────────────────────── 보조 지표 ─────────────────────────
def rsi(c, n):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def cross_up(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def base(d):
    c, v = d["Close"], d["Volume"]
    amt20 = (c * v).rolling(20).mean()
    return (c >= 1000) & (amt20 >= 20 * EOK)


def vol_avg(v, n=20):
    return v.rolling(n).mean().shift(1)   # 오늘 제외 직전 n일 평균


# ───────────────────────── 조건식 10종 ─────────────────────────
# 각 함수: (매수신호, 매도신호) 반환. 매도신호는 그날 종가 기준 → 다음날 시가 매도
def b01_52w_momentum(d):
    """01 52주 신고가 모멘텀: 52주 최고가 5% 이내 + 정배열"""
    h, c = d["High"], d["Close"]
    m20, m60, m120 = ma(c, 20), ma(c, 60), ma(c, 120)
    buy = (c >= h.rolling(250).max() * 0.95) & (m20 > m60) & (m60 > m120) & (c > m20)
    return buy, c < m20


def b02_vcp_breakout(d):
    """02 변동성 축소 돌파(VCP): 20일 박스(폭 15%이내)를 거래량 2배로 돌파, 200일선 위"""
    h, l, c, v = d["High"], d["Low"], d["Close"], d["Volume"]
    hh, ll = h.rolling(20).max().shift(1), l.rolling(20).min().shift(1)
    buy = ((hh / ll - 1) <= 0.15) & (c > hh) & (v >= vol_avg(v) * 2) & \
          (c > ma(c, 50)) & (ma(c, 50) > ma(c, 200))
    return buy, c < ma(c, 10)


def b03_rsi2_dip(d):
    """03 상승추세 단기 과매도: 200일선 위 + RSI(2) 10 이하 → 5일선 회복 시 매도"""
    c = d["Close"]
    buy = (c > ma(c, 200)) & (rsi(c, 2) <= 10)
    return buy, c > ma(c, 5)


def b04_bollinger_dip(d):
    """04 볼린저 하단 반등: 60일선 상승 + 120일선 위 + 저가가 볼린저(20,2) 하단 터치 후 양봉"""
    o, l, c = d["Open"], d["Low"], d["Close"]
    m20 = ma(c, 20)
    lower = m20 - 2 * c.rolling(20).std()
    m60 = ma(c, 60)
    buy = (m60 > m60.shift(5)) & (c > ma(c, 120)) & (l <= lower) & (c > o)
    return buy, c >= m20


def b05_converge_volume(d):
    """05 이평 수렴 + 거래량 양봉: 20/60/120일선 5% 이내 밀집 + 거래량 1.5배 양봉"""
    o, c, v = d["Open"], d["Close"], d["Volume"]
    m20, m60, m120 = ma(c, 20), ma(c, 60), ma(c, 120)
    conv = ((m20 / m60 - 1).abs() <= 0.05) & ((m20 / m120 - 1).abs() <= 0.05) & \
           ((m60 / m120 - 1).abs() <= 0.05)
    buy = conv & (c > m20) & (c > o) & (v >= vol_avg(v) * 1.5) & (c / c.shift(1) - 1 <= 0.08)
    return buy, c < m20


def b06_leader_pullback(d):
    """06 주도주 첫 눌림: 10일 내 60일 신고가 + 거래대금 300억 → 10일선 지지 양봉"""
    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]
    m10 = ma(c, 10)
    buy = within(new_high(h, 60), 10) & within(c * v >= 300 * EOK, 10) & \
          (l <= m10 * 1.02) & (c >= m10) & (c > o) & (c / c.shift(1) - 1 <= 0.05) & \
          (h < h.rolling(10).max())     # 오늘은 신고가 아님(눌림 상태)
    return buy, c < m10


def b07_golden_cross(d):
    """07 20/60 골든크로스 + 거래량 1.5배 + 120일선 상승"""
    c, v = d["Close"], d["Volume"]
    m20, m60, m120 = ma(c, 20), ma(c, 60), ma(c, 120)
    buy = cross_up(m20, m60) & (v >= vol_avg(v) * 1.5) & (m120 > m120.shift(5))
    return buy, c < m60


def b08_box_breakout(d):
    """08 60일 박스권 돌파: 60일 고저폭 30% 이내 박스 상단을 거래량 2배로 돌파"""
    h, l, c, v = d["High"], d["Low"], d["Close"], d["Volume"]
    hh, ll = h.rolling(60).max().shift(1), l.rolling(60).min().shift(1)
    buy = ((hh / ll - 1) <= 0.30) & (c > hh) & (v >= vol_avg(v) * 2)
    return buy, c < ma(c, 20)


def b09_lowvol_trend(d):
    """09 저변동성 우상향: 60일 고저폭 25% 이내 + 60일선 상승 + 20일 고점 3% 이내, 거래대금 50억"""
    h, l, c, v = d["High"], d["Low"], d["Close"], d["Volume"]
    m60 = ma(c, 60)
    buy = ((h.rolling(60).max() / l.rolling(60).min() - 1) <= 0.25) & (c > m60) & \
          (m60 > m60.shift(10)) & (c >= h.rolling(20).max() * 0.97) & \
          ((c * v).rolling(20).mean() >= 50 * EOK)
    return buy, c < m60


def b10_bottom_escape(d):
    """10 장기 바닥 탈출: 120일선 상향 돌파 + 120일선 상승 전환 + 거래량 3배 + 52주 저점 대비 50% 이내"""
    l, c, v = d["Low"], d["Close"], d["Volume"]
    m120 = ma(c, 120)
    buy = cross_up(c, m120) & (m120 > m120.shift(5)) & (v >= vol_avg(v) * 3) & \
          (c <= l.rolling(250).min() * 1.5)
    return buy, c < ma(c, 20)


# 이름, 함수, 손절, 최대보유(거래일)
STRATS = [
    ("01 52주 신고가 모멘텀", b01_52w_momentum, -0.08, 60),
    ("02 변동성축소 돌파(VCP)", b02_vcp_breakout, -0.07, 40),
    ("03 상승추세 RSI(2) 과매도", b03_rsi2_dip, -0.10, 10),
    ("04 볼린저 하단 반등", b04_bollinger_dip, -0.07, 15),
    ("05 이평수렴+거래량 양봉", b05_converge_volume, -0.07, 40),
    ("06 주도주 첫 눌림", b06_leader_pullback, -0.06, 20),
    ("07 20/60 골든크로스", b07_golden_cross, -0.08, 60),
    ("08 60일 박스권 돌파", b08_box_breakout, -0.07, 40),
    ("09 저변동성 우상향", b09_lowvol_trend, -0.08, 60),
    ("10 장기 바닥 탈출", b10_bottom_escape, -0.08, 40),
]


# ───────────────────────── 시장 필터 ─────────────────────────
def load_market_filter(prices, uni, demo=False):
    """지수 종가 > 20일선 여부 (시장별). 지수 못 받으면 해당 시장 종목들로 동일가중 지수 생성"""
    out = {}
    for m, sym in [("KOSPI", "KS11"), ("KOSDAQ", "KQ11")]:
        idx = None
        if not demo:
            try:
                import FinanceDataReader as fdr
                idx = fdr.DataReader(sym, "2016-01-01")["Close"]
                print(f"[지수] {sym} {len(idx)}일", flush=True)
            except Exception as e:
                print(f"[지수] {sym} 실패({e}) → 동일가중 지수로 대체", flush=True)
        if idx is None or len(idx) < 100:
            codes = uni.loc[uni["Market"] == m, "Code"]
            rets = pd.DataFrame({c: prices[c]["Close"].pct_change() for c in codes if c in prices})
            idx = (1 + rets.median(axis=1).fillna(0)).cumprod()
        out[m] = idx > ma(idx, 20)
    return out


# ───────────────────────── 시뮬레이션 ─────────────────────────
def simulate(d, buy, sell, sl, hold):
    o, h, l, c = (d[k].to_numpy() for k in ["Open", "High", "Low", "Close"])
    amt = (d["Close"] * d["Volume"]).to_numpy()
    idx = d.index
    buy, sell = buy.fillna(False).to_numpy(), sell.fillna(False).to_numpy()
    n = len(d)
    i = idx.searchsorted(pd.Timestamp(TEST_START))
    trades = []
    while i < n - 1:
        if not buy[i]:
            i += 1
            continue
        e = i + 1
        entry = o[e]
        stop = entry * (1 + sl)
        exit_p, reason, x = None, None, None
        for j in range(e, n):
            if j > e and o[j] <= stop:
                exit_p, reason, x = o[j], "손절", j; break
            if l[j] <= stop:
                exit_p, reason, x = stop, "손절", j; break
            if j - e + 1 >= hold:
                exit_p, reason, x = c[j], "기간만료", j; break
            if sell[j] and j + 1 < n:
                exit_p, reason, x = o[j + 1], "추세이탈", j + 1; break
        if exit_p is None:          # 데이터 끝까지 보유 중 → 마지막 종가로 평가
            x = n - 1
            exit_p, reason = c[x], "보유중"
        trades.append((idx[i], idx[e], idx[x], entry, exit_p, exit_p / entry - 1 - COST, reason, amt[i]))
        i = x + 1
    return trades


def portfolio(t, slots=SLOTS):
    """동시 보유 최대 slots종목, 매수 시 현재 자산의 1/slots 투입. 같은 날 신호는 거래대금 큰 순."""
    if t.empty:
        return {}
    t = t.sort_values(["매수일", "거래대금"], ascending=[True, False])
    cash, equity_curve, open_pos = 1.0, [], []   # open_pos: (매도일, 투입금, 수익률)
    taken = 0
    for _, r in t.iterrows():
        # 매수일 이전에 끝난 포지션 정산
        still = []
        for ex, alloc, ret in open_pos:
            if ex < r["매수일"]:
                cash += alloc * (1 + ret)
            else:
                still.append((ex, alloc, ret))
        open_pos = still
        if len(open_pos) >= slots:
            continue
        nav = cash + sum(a for _, a, _ in open_pos)
        alloc = min(nav / slots, cash)
        if alloc <= 0:
            continue
        cash -= alloc
        open_pos.append((r["매도일"], alloc, r["수익률"]))
        taken += 1
        equity_curve.append((r["매수일"], nav))
    for ex, alloc, ret in sorted(open_pos):
        cash += alloc * (1 + ret)
    eq = pd.Series([v for _, v in equity_curve], index=[d for d, _ in equity_curve])
    eq = pd.concat([eq, pd.Series([cash], index=[t["매도일"].max()])])
    years = (eq.index[-1] - pd.Timestamp(TEST_START)).days / 365.25
    cagr = cash ** (1 / years) - 1 if years > 0 else np.nan
    mdd = (eq / eq.cummax() - 1).min()
    yearly = eq.groupby(eq.index.year).last()
    prev = yearly.shift(1).fillna(1.0)
    return {"최종자산(배)": round(cash, 2), "연평균%": round(cagr * 100, 1),
            "최대낙폭%": round(mdd * 100, 1), "실제매수": taken,
            "연도별%": ((yearly / prev - 1) * 100).round(1).to_dict()}


def stats(t):
    if t.empty:
        return dict(거래수=0)
    r = t["수익률"]
    w, lo = r[r > 0], r[r <= 0]
    return dict(거래수=len(r), 승률=round((r > 0).mean() * 100, 1), 평균=round(r.mean() * 100, 2),
                평균이익=round(w.mean() * 100, 1) if len(w) else 0,
                평균손실=round(lo.mean() * 100, 1) if len(lo) else 0,
                PF=round(w.sum() / -lo.sum(), 2) if lo.sum() < 0 else np.inf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    if args.demo:
        uni, prices = make_demo()
    else:
        uni = load_universe()
        if args.limit:
            uni = uni.head(args.limit)
        prices = load_prices(uni["Code"].tolist())
    mkt = load_market_filter(prices, uni, args.demo)
    market_of = uni.set_index("Code")["Market"].to_dict()
    names = uni.set_index("Code")["Name"].to_dict()
    os.makedirs(OUT_DIR, exist_ok=True)

    rows, yearly = [], {}
    cols = ["신호일", "매수일", "매도일", "매수가", "매도가", "수익률", "사유", "거래대금"]
    for name, fn, sl, hold in STRATS:
        for use_filter in (True, False):
            allt = []
            for code, d in prices.items():
                try:
                    buy, sell = fn(d)
                    buy = buy & base(d)
                    if use_filter:
                        f = mkt[market_of.get(code, "KOSDAQ")].reindex(d.index).fillna(False)
                        buy = buy & f
                    for tr in simulate(d, buy, sell, sl, hold):
                        allt.append((code, names.get(code, ""), *tr))
                except Exception as e:
                    print(f"  {name} {code} 오류: {e}", flush=True)
            t = pd.DataFrame(allt, columns=["종목코드", "종목명"] + cols)
            tag = name + ("" if use_filter else " [시장필터 없음]")
            a, b = stats(t), {}
            if not t.empty:
                b1, b2 = stats(t[t["신호일"] < SPLIT]), stats(t[t["신호일"] >= SPLIT])
                hd = (t["매도일"] - t["매수일"]).dt.days.mean()
            else:
                b1 = b2 = {}; hd = np.nan
            pf = portfolio(t)
            rows.append({"전략": tag, "거래수": a.get("거래수"), "승률%": a.get("승률"),
                         "평균%": a.get("평균"), "평균이익%": a.get("평균이익"),
                         "평균손실%": a.get("평균손실"), "PF": a.get("PF"),
                         "전반기(20~23)평균%": b1.get("평균"), "후반기(24~26)평균%": b2.get("평균"),
                         "보유(달력일)": round(hd, 1),
                         "포트_연평균%": pf.get("연평균%"), "포트_최대낙폭%": pf.get("최대낙폭%"),
                         "포트_최종(배)": pf.get("최종자산(배)")})
            yearly[tag] = pf.get("연도별%", {})
            if use_filter:
                t.sort_values("신호일").to_csv(os.path.join(OUT_DIR, f"trades_{name[:2]}.csv"),
                                               index=False, encoding="utf-8-sig")
            print(f"[완료] {tag}: {a.get('거래수')}건 평균 {a.get('평균')}% 포트 {pf.get('연평균%')}%", flush=True)

    sm = pd.DataFrame(rows)
    sm.to_csv(os.path.join(OUT_DIR, "summary.csv"), index=False, encoding="utf-8-sig")
    yr = pd.DataFrame(yearly).T
    lines = ["# 새 조건검색식 10종 백테스트", "",
             f"- 실행: {pd.Timestamp.now(tz='Asia/Seoul'):%Y-%m-%d %H:%M} KST / 대상 {len(prices)}종목 / {TEST_START}~",
             "- 공통: 주가 1,000원↑, 20일 평균 거래대금 20억↑ / 매수 = 신호 다음날 시가 / 비용 왕복 0.25%",
             "- 시장필터: 해당 지수 종가 > 20일선일 때만 매수",
             "- 매도: 손절가 / 추세이탈(종가 기준 → 다음날 시가) / 최대 보유일",
             f"- 포트 = 동시 최대 {SLOTS}종목, 종목당 자산의 1/{SLOTS}, 같은 날 신호 많으면 거래대금 큰 순",
             "- 상장폐지 종목 미포함 → 실제보다 다소 좋게 나올 수 있음", "",
             "## 요약", "", sm.to_markdown(index=False), "",
             "## 포트폴리오 연도별 수익률(%)", "", yr.to_markdown(), "",
             f"소요 {time.time()-t0:.0f}초"]
    with open(os.path.join(OUT_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
