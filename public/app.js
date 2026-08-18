"use strict";

/* ============================================================
   State
   ============================================================ */
const state = {
  ticker: null, // {symbol, name, currency}
  historyCache: new Map(), // "SYMBOL|start|end" -> {rows, currency, name}
  customStrategies: [], // user-built strategies
  enabledIds: new Set(), // ids of builtin+custom strategies currently checked
  watchlist: [], // [{symbol, name, currency}]
  lastRun: null, // last single-ticker run result, used for share link + chart
};

const $ = (id) => document.getElementById(id);

/* ============================================================
   Small utilities
   ============================================================ */
function debounce(fn, wait) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function isoToDate(s) {
  return new Date(`${s}T00:00:00Z`);
}

function todayStr() {
  return new Date().toISOString().slice(0, 10);
}

function yearsAgoStr(n) {
  const d = new Date();
  d.setUTCFullYear(d.getUTCFullYear() - n);
  return d.toISOString().slice(0, 10);
}

function fmtMoney(v, currency) {
  if (!isFinite(v)) return "-";
  if (currency === "KRW") {
    return `${Math.round(v / 10000).toLocaleString("ko-KR")}만원`;
  }
  return `$${Math.round(v).toLocaleString("en-US")}`;
}

function fmtPct(v, withSign) {
  if (!isFinite(v)) return "-";
  const sign = withSign && v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}%`;
}

/* ============================================================
   API
   ============================================================ */
async function apiSearch(q) {
  const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  if (!r.ok) return [];
  const data = await r.json();
  return data.results || [];
}

async function apiHistory(symbol, start, end) {
  const key = `${symbol}|${start}|${end}`;
  if (state.historyCache.has(key)) return state.historyCache.get(key);
  const r = await fetch(
    `/api/history?symbol=${encodeURIComponent(symbol)}&start=${start}&end=${end}`
  );
  const data = await r.json();
  if (!r.ok || data.error) {
    throw new Error(data.error || "가격 데이터를 가져오지 못했습니다.");
  }
  if (!data.rows || data.rows.length < 30) {
    throw new Error("이 기간에 대한 가격 데이터가 충분하지 않습니다.");
  }
  state.historyCache.set(key, data);
  return data;
}

/* ============================================================
   Indicators / simulation context
   ============================================================ */
function computeSMA(values, n) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= n) sum -= values[i - n];
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

function computeRSI(values, period) {
  const out = new Array(values.length).fill(null);
  if (values.length <= period) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const diff = values[i] - values[i - 1];
    if (diff >= 0) gain += diff; else loss -= diff;
  }
  let avgGain = gain / period, avgLoss = loss / period;
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  for (let i = period + 1; i < values.length; i++) {
    const diff = values[i] - values[i - 1];
    const g = diff > 0 ? diff : 0;
    const l = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (period - 1) + g) / period;
    avgLoss = (avgLoss * (period - 1) + l) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

function computePctChange(values) {
  const out = new Array(values.length).fill(null);
  for (let i = 1; i < values.length; i++) {
    out[i] = ((values[i] - values[i - 1]) / values[i - 1]) * 100;
  }
  return out;
}

function buildMonthGroups(rows) {
  const groups = [];
  let cur = null, curKey = null;
  rows.forEach((r, i) => {
    const key = r.date.slice(0, 7);
    if (key !== curKey) {
      cur = { key, indices: [] };
      groups.push(cur);
      curKey = key;
    }
    cur.indices.push(i);
  });
  return groups;
}

function buildCtx(rows) {
  const closes = rows.map((r) => r.adjclose);
  const weekday = rows.map((r) => isoToDate(r.date).getUTCDay());
  const pctChange = computePctChange(closes);
  const rsi14 = computeRSI(closes, 14);
  const monthGroups = buildMonthGroups(rows);

  const isFirstOfMonth = new Array(rows.length).fill(false);
  const isLastOfMonth = new Array(rows.length).fill(false);
  monthGroups.forEach((g) => {
    isFirstOfMonth[g.indices[0]] = true;
    isLastOfMonth[g.indices[g.indices.length - 1]] = true;
  });

  const smaCache = new Map();
  const monthDeployCache = new Map();

  return {
    rows,
    weekday,
    pctChange,
    rsi14,
    monthGroups,
    isFirstOfMonth,
    isLastOfMonth,
    getSMA(n) {
      if (!smaCache.has(n)) smaCache.set(n, computeSMA(closes, n));
      return smaCache.get(n);
    },
    getMonthDeploy(targetDay) {
      if (!monthDeployCache.has(targetDay)) {
        const flags = new Array(rows.length).fill(false);
        monthGroups.forEach((g) => {
          let chosen = null;
          for (const i of g.indices) {
            const d = parseInt(rows[i].date.slice(8, 10), 10);
            if (d >= targetDay) { chosen = i; break; }
          }
          if (chosen === null) chosen = g.indices[g.indices.length - 1];
          flags[chosen] = true;
        });
        monthDeployCache.set(targetDay, flags);
      }
      return monthDeployCache.get(targetDay);
    },
  };
}

/* ============================================================
   Strategy definitions
   ============================================================ */
function buildBuiltinStrategies(payday) {
  const pd = payday || 25;
  return [
    { id: "daily", label: "매일 적립", group: "freq",
      qualifies: () => true },
    { id: "weekly_fri", label: "매주 금요일", group: "freq",
      qualifies: (ctx, i) => ctx.weekday[i] === 5 },
    { id: "monthly_payday", label: `매월 ${pd}일`, group: "freq", isBaseline: true,
      qualifies: (ctx, i) => ctx.getMonthDeploy(pd)[i] },
    { id: "monthly_15", label: "매월 15일", group: "freq",
      qualifies: (ctx, i) => ctx.getMonthDeploy(15)[i] },
    { id: "monthly_first", label: "매월 첫 거래일", group: "freq",
      qualifies: (ctx, i) => ctx.isFirstOfMonth[i] },
    { id: "monthly_last", label: "매월 마지막 거래일", group: "freq",
      qualifies: (ctx, i) => ctx.isLastOfMonth[i] },
    { id: "ma10", label: "MA10 아래일 때만", group: "cond",
      qualifies: (ctx, i) => { const s = ctx.getSMA(10)[i]; return s != null && ctx.rows[i].adjclose < s; } },
    { id: "ma50", label: "MA50 아래일 때만", group: "cond",
      qualifies: (ctx, i) => { const s = ctx.getSMA(50)[i]; return s != null && ctx.rows[i].adjclose < s; } },
    { id: "ma100", label: "MA100 아래일 때만", group: "cond",
      qualifies: (ctx, i) => { const s = ctx.getSMA(100)[i]; return s != null && ctx.rows[i].adjclose < s; } },
    { id: "ma200", label: "MA200 아래일 때만", group: "cond",
      qualifies: (ctx, i) => { const s = ctx.getSMA(200)[i]; return s != null && ctx.rows[i].adjclose < s; } },
    { id: "rsi30", label: "RSI(14) 30 이하일 때만", group: "cond",
      qualifies: (ctx, i) => ctx.rsi14[i] != null && ctx.rsi14[i] <= 30 },
    { id: "rsi20", label: "RSI(14) 20 이하일 때만", group: "cond",
      qualifies: (ctx, i) => ctx.rsi14[i] != null && ctx.rsi14[i] <= 20 },
    { id: "drop3", label: "전일 대비 -3% 이상 하락 시", group: "cond",
      qualifies: (ctx, i) => ctx.pctChange[i] != null && ctx.pctChange[i] <= -3 },
    { id: "drop5", label: "전일 대비 -5% 이상 하락 시", group: "cond",
      qualifies: (ctx, i) => ctx.pctChange[i] != null && ctx.pctChange[i] <= -5 },
  ];
}

const FILTER_LABELS = {
  none: "조건 없음",
  ma_below: (n) => `MA${n} 아래`,
  ma_above: (n) => `MA${n} 위`,
  rsi_le: (t) => `RSI ${t} 이하`,
  rsi_ge: (t) => `RSI ${t} 이상`,
  drop_ge: (t) => `전일比 -${t}% 이상 하락`,
  rise_ge: (t) => `전일比 +${t}% 이상 상승`,
};

function makeFilterFn(type, value) {
  switch (type) {
    case "ma_below": return (ctx, i) => { const s = ctx.getSMA(value)[i]; return s != null && ctx.rows[i].adjclose < s; };
    case "ma_above": return (ctx, i) => { const s = ctx.getSMA(value)[i]; return s != null && ctx.rows[i].adjclose > s; };
    case "rsi_le": return (ctx, i) => ctx.rsi14[i] != null && ctx.rsi14[i] <= value;
    case "rsi_ge": return (ctx, i) => ctx.rsi14[i] != null && ctx.rsi14[i] >= value;
    case "drop_ge": return (ctx, i) => ctx.pctChange[i] != null && ctx.pctChange[i] <= -value;
    case "rise_ge": return (ctx, i) => ctx.pctChange[i] != null && ctx.pctChange[i] >= value;
    default: return null;
  }
}

const WEEKDAY_NAMES = ["일", "월", "화", "수", "목", "금", "토"];

function buildCustomStrategy(cfg) {
  let baseFn, baseLabel;
  if (cfg.base === "daily") { baseFn = () => true; baseLabel = "매일 체크"; }
  else if (cfg.base === "weekday") { baseFn = (ctx, i) => ctx.weekday[i] === cfg.weekday; baseLabel = `매주 ${WEEKDAY_NAMES[cfg.weekday]}요일`; }
  else if (cfg.base === "month_day") { baseFn = (ctx, i) => ctx.getMonthDeploy(cfg.day)[i]; baseLabel = `매월 ${cfg.day}일`; }
  else if (cfg.base === "month_first") { baseFn = (ctx, i) => ctx.isFirstOfMonth[i]; baseLabel = "매월 첫 거래일"; }
  else if (cfg.base === "month_last") { baseFn = (ctx, i) => ctx.isLastOfMonth[i]; baseLabel = "매월 마지막 거래일"; }

  const f1 = cfg.filter1 && cfg.filter1.type !== "none" ? makeFilterFn(cfg.filter1.type, cfg.filter1.value) : null;
  const f2 = cfg.filter2 && cfg.filter2.type !== "none" ? makeFilterFn(cfg.filter2.type, cfg.filter2.value) : null;

  let qualifies;
  if (f1 && f2) {
    qualifies = cfg.op === "OR"
      ? (ctx, i) => baseFn(ctx, i) && (f1(ctx, i) || f2(ctx, i))
      : (ctx, i) => baseFn(ctx, i) && f1(ctx, i) && f2(ctx, i);
  } else if (f1) {
    qualifies = (ctx, i) => baseFn(ctx, i) && f1(ctx, i);
  } else {
    qualifies = baseFn;
  }

  const parts = [baseLabel];
  if (cfg.filter1 && cfg.filter1.type !== "none") parts.push(FILTER_LABELS[cfg.filter1.type](cfg.filter1.value));
  if (cfg.filter2 && cfg.filter2.type !== "none") parts.push((cfg.op === "OR" ? "또는 " : "+ ") + FILTER_LABELS[cfg.filter2.type](cfg.filter2.value));
  const label = parts.join(" · ");

  return { id: `custom_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`, label, group: "custom", qualifies };
}

/* ============================================================
   Simulation
   ============================================================ */
function simulateStrategy(ctx, strategy, monthlyBudget, feePct, withCurve) {
  const rows = ctx.rows;
  const investedAmount = new Float64Array(rows.length);

  for (const g of ctx.monthGroups) {
    const qual = [];
    for (const i of g.indices) if (strategy.qualifies(ctx, i)) qual.push(i);
    if (qual.length) {
      const amt = monthlyBudget / qual.length;
      for (const i of qual) investedAmount[i] = amt;
    }
  }

  let shares = 0, invested = 0;
  const curve = withCurve ? new Array(rows.length) : null;
  for (let i = 0; i < rows.length; i++) {
    if (investedAmount[i] > 0) {
      shares += (investedAmount[i] * (1 - feePct / 100)) / rows[i].adjclose;
      invested += investedAmount[i];
    }
    if (withCurve) {
      const value = shares * rows[i].adjclose;
      curve[i] = { date: rows[i].date, returnPct: invested > 0 ? (value / invested - 1) * 100 : 0 };
    }
  }

  const lastPrice = rows[rows.length - 1].adjclose;
  const finalValue = shares * lastPrice;
  const returnPct = invested > 0 ? (finalValue / invested - 1) * 100 : 0;
  const years = (isoToDate(rows[rows.length - 1].date) - isoToDate(rows[0].date)) / (365.25 * 86400 * 1000);
  const annualizedPct = invested > 0 && years > 0.05 ? (Math.pow(finalValue / invested, 1 / years) - 1) * 100 : returnPct;

  return { strategy, shares, invested, finalValue, returnPct, annualizedPct, curve };
}

/* ============================================================
   Settings helpers
   ============================================================ */
function getSettings() {
  return {
    start: $("startDate").value,
    end: $("endDate").value,
    monthlyBudget: parseFloat($("monthlyBudget").value) || 1000000,
    payday: Math.max(1, Math.min(28, parseInt($("payday").value, 10) || 25)),
    feePct: parseFloat($("feePct").value) || 0,
  };
}

function getAllStrategies() {
  return [...buildBuiltinStrategies(getSettings().payday), ...state.customStrategies];
}

function getEnabledStrategies() {
  return getAllStrategies().filter((s) => state.enabledIds.has(s.id));
}

/* ============================================================
   Ticker search (shared by both tabs)
   ============================================================ */
function attachTickerSearch(inputEl, resultsEl, onSelect) {
  const doSearch = debounce(async (q) => {
    if (!q || q.trim().length < 1) { resultsEl.style.display = "none"; return; }
    let items = [];
    try { items = await apiSearch(q.trim()); } catch { items = []; }

    if (/^\d{6}$/.test(q.trim())) {
      items = items.concat([
        { symbol: `${q.trim()}.KS`, name: `코드 ${q.trim()} · 코스피(KOSPI) 직접 조회`, exchange: "KOSPI", manual: true },
        { symbol: `${q.trim()}.KQ`, name: `코드 ${q.trim()} · 코스닥(KOSDAQ) 직접 조회`, exchange: "KOSDAQ", manual: true },
      ]);
    }

    if (!items.length) {
      resultsEl.innerHTML = `<div class="sr-item" style="cursor:default;"><span class="sr-sub">검색 결과가 없습니다. 정확한 티커(symbol)를 입력 후 Enter를 눌러보세요.</span></div>`;
      resultsEl.style.display = "block";
      return;
    }

    resultsEl.innerHTML = items.map((it, idx) => `
      <div class="sr-item" data-idx="${idx}">
        <div>
          <div class="sr-name">${escapeHtml(it.name)}</div>
          <div class="sr-sub">${escapeHtml(it.exchange || it.type || "")}</div>
        </div>
        <div class="sr-symbol">${escapeHtml(it.symbol)}</div>
      </div>`).join("");
    resultsEl.style.display = "block";

    resultsEl.querySelectorAll(".sr-item[data-idx]").forEach((el) => {
      el.addEventListener("click", () => {
        const it = items[parseInt(el.dataset.idx, 10)];
        resultsEl.style.display = "none";
        inputEl.value = "";
        onSelect({ symbol: it.symbol, name: it.name.replace(/\s·.*$/, ""), currency: /\.(ks|kq)$/i.test(it.symbol) ? "KRW" : "USD" });
      });
    });
  }, 300);

  inputEl.addEventListener("input", () => doSearch(inputEl.value));
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const raw = inputEl.value.trim();
      if (!raw) return;
      resultsEl.style.display = "none";
      const symbol = /[.\d]/.test(raw) && raw.includes(".") ? raw : (/^\d+$/.test(raw) ? raw : raw.toUpperCase());
      inputEl.value = "";
      onSelect({ symbol, name: raw, currency: /\.(ks|kq)$/i.test(symbol) ? "KRW" : "USD" });
    }
  });
  document.addEventListener("click", (e) => {
    if (!inputEl.contains(e.target) && !resultsEl.contains(e.target)) resultsEl.style.display = "none";
  });
}

/* ============================================================
   Tab 1: single ticker UI
   ============================================================ */
function renderSelectedTicker() {
  const wrap = $("selectedTickerWrap");
  if (!state.ticker) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `
    <div class="selected-ticker">
      <span>${escapeHtml(state.ticker.name)} <span style="color:var(--text-muted); font-weight:500;">(${escapeHtml(state.ticker.symbol)})</span></span>
      <button type="button" id="clearTickerBtn" aria-label="선택 해제">✕</button>
    </div>`;
  $("clearTickerBtn").addEventListener("click", () => { state.ticker = null; renderSelectedTicker(); });
}

function renderStrategyList() {
  const container = $("strategyList");
  const builtins = buildBuiltinStrategies(getSettings().payday);
  const freq = builtins.filter((s) => s.group === "freq");
  const cond = builtins.filter((s) => s.group === "cond");

  const renderGroup = (title, list) => `
    <div class="strategy-group-title">${title}</div>
    ${list.map((s) => `
      <label class="strategy-item">
        <input type="checkbox" data-id="${s.id}" ${state.enabledIds.has(s.id) ? "checked" : ""} />
        <span class="si-label">${escapeHtml(s.label)}${s.isBaseline ? ' <span class="r-badge" style="margin-left:4px;">월급날 기준</span>' : ""}</span>
      </label>`).join("")}`;

  let html = renderGroup("빈도 기반 전략", freq) + renderGroup("조건 기반 전략 (이동평균 · RSI · 급락)", cond);

  if (state.customStrategies.length) {
    html += `<div class="strategy-group-title">직접 추가한 전략</div>` +
      state.customStrategies.map((s) => `
        <label class="strategy-item">
          <input type="checkbox" data-id="${s.id}" ${state.enabledIds.has(s.id) ? "checked" : ""} />
          <span class="si-label">${escapeHtml(s.label)}</span>
          <button type="button" class="si-remove" data-remove="${s.id}">삭제</button>
        </label>`).join("");
  }

  container.innerHTML = html;

  container.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) state.enabledIds.add(cb.dataset.id);
      else state.enabledIds.delete(cb.dataset.id);
    });
  });
  container.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.remove;
      state.customStrategies = state.customStrategies.filter((s) => s.id !== id);
      state.enabledIds.delete(id);
      renderStrategyList();
    });
  });
}

function initDefaultEnabled() {
  buildBuiltinStrategies(25).forEach((s) => state.enabledIds.add(s.id));
}

/* ---------- custom strategy builder wiring ---------- */
function renderFilterParamRow(rowEl, type, prefix) {
  if (!type || type === "none") { rowEl.style.display = "none"; rowEl.innerHTML = ""; return; }
  rowEl.style.display = "grid";
  if (type === "ma_below" || type === "ma_above") {
    rowEl.innerHTML = `<div class="field" style="grid-column:1/-1;"><label>이동평균 기간(일)</label><input type="number" id="${prefix}Value" value="50" min="2" max="300" /></div>`;
  } else if (type === "rsi_le" || type === "rsi_ge") {
    rowEl.innerHTML = `<div class="field" style="grid-column:1/-1;"><label>RSI(14) 기준값</label><input type="number" id="${prefix}Value" value="${type === "rsi_le" ? 30 : 70}" min="1" max="99" /></div>`;
  } else {
    rowEl.innerHTML = `<div class="field" style="grid-column:1/-1;"><label>전일 대비 등락률 기준(%)</label><input type="number" id="${prefix}Value" value="3" min="0.1" max="30" step="0.1" /></div>`;
  }
}

function wireCustomBuilder() {
  $("toggleCustomBuilder").addEventListener("click", () => {
    const form = $("customBuilderForm");
    form.style.display = form.style.display === "none" ? "block" : "none";
  });

  $("cbBase").addEventListener("change", () => {
    const v = $("cbBase").value;
    $("cbWeekdayField").style.display = v === "weekday" ? "block" : "none";
    $("cbDayField").style.display = v === "month_day" ? "block" : "none";
    $("cbBaseParamRow").style.display = (v === "weekday" || v === "month_day") ? "grid" : "none";
  });

  $("cbFilter1").addEventListener("change", () => {
    renderFilterParamRow($("cbFilter1ParamRow"), $("cbFilter1").value, "cbF1");
    const has2 = $("cbFilter1").value !== "none";
    $("cbOpField").style.display = has2 ? "block" : "none";
    $("cbFilter2Wrap").style.display = has2 ? "block" : "none";
    if (!has2) { $("cbFilter2").value = "none"; renderFilterParamRow($("cbFilter2ParamRow"), "none", "cbF2"); }
  });
  $("cbFilter2").addEventListener("change", () => {
    renderFilterParamRow($("cbFilter2ParamRow"), $("cbFilter2").value, "cbF2");
  });

  $("cbAddBtn").addEventListener("click", () => {
    const cfg = {
      base: $("cbBase").value,
      weekday: parseInt($("cbWeekday").value, 10),
      day: Math.max(1, Math.min(28, parseInt($("cbDay").value, 10) || 10)),
      filter1: { type: $("cbFilter1").value, value: parseFloat($("cbF1Value")?.value) || 0 },
      filter2: { type: $("cbFilter2").value, value: parseFloat($("cbF2Value")?.value) || 0 },
      op: $("cbOp").value,
    };
    const strategy = buildCustomStrategy(cfg);
    state.customStrategies.push(strategy);
    state.enabledIds.add(strategy.id);
    renderStrategyList();
    $("customBuilderForm").style.display = "none";
  });
}

/* ---------- period presets ---------- */
function wirePeriodPresets() {
  const presetRow = $("periodPresets");
  presetRow.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      presetRow.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      $("endDate").value = todayStr();
      $("startDate").value = btn.dataset.years === "max" ? "1990-01-01" : yearsAgoStr(parseInt(btn.dataset.years, 10));
      renderStrategyList();
    });
  });
}

/* ============================================================
   Run: single ticker backtest
   ============================================================ */
async function runSingleBacktest() {
  if (!state.ticker) { alert("먼저 종목을 선택해주세요."); return; }
  const enabled = getEnabledStrategies();
  if (!enabled.length) { alert("최소 1개 이상의 전략을 선택해주세요."); return; }

  const settings = getSettings();
  const card = $("singleResultsCard");
  card.style.display = "block";
  $("singleStatus").innerHTML = `<div class="status-msg"><span class="spinner" style="border-color:rgba(0,0,0,.15); border-top-color:var(--series-1);"></span>가격 데이터를 불러오는 중...</div>`;
  $("singleChartArea").style.display = "none";
  $("singleResultsList").innerHTML = "";
  $("shareRow").style.display = "none";
  $("runSingleBtn").disabled = true;

  try {
    const data = await apiHistory(state.ticker.symbol, settings.start, settings.end);
    state.ticker.currency = data.currency || state.ticker.currency;
    state.ticker.name = data.name && data.name !== state.ticker.symbol ? data.name : state.ticker.name;
    const ctx = buildCtx(data.rows);

    const results = enabled.map((s) => simulateStrategy(ctx, s, settings.monthlyBudget, settings.feePct, false));
    results.sort((a, b) => b.returnPct - a.returnPct);

    const baselineStrategy = getAllStrategies().find((s) => s.isBaseline);
    const baselineResult = baselineStrategy
      ? (results.find((r) => r.strategy.id === baselineStrategy.id) || simulateStrategy(ctx, baselineStrategy, settings.monthlyBudget, settings.feePct, false))
      : null;

    const top1 = results[0];
    const topCurveResult = simulateStrategy(ctx, top1.strategy, settings.monthlyBudget, settings.feePct, true);
    const baseCurveResult = baselineStrategy && baselineStrategy.id !== top1.strategy.id
      ? simulateStrategy(ctx, baselineStrategy, settings.monthlyBudget, settings.feePct, true)
      : (baselineStrategy ? topCurveResult : null);

    state.lastRun = { symbol: state.ticker.symbol, name: state.ticker.name, currency: data.currency, settings, results, top1, baselineResult, rows: data.rows };

    $("singleStatus").innerHTML = "";
    renderSingleResults(results, baselineResult, data.currency, top1);
    if (topCurveResult && baseCurveResult) {
      renderChart(topCurveResult, baseCurveResult, data.rows, data.currency);
      $("singleChartArea").style.display = "block";
    }
    $("shareRow").style.display = "flex";
    updateShareUrl();
  } catch (e) {
    $("singleStatus").innerHTML = `<div class="status-msg error">⚠️ ${escapeHtml(e.message || String(e))}</div>`;
  } finally {
    $("runSingleBtn").disabled = false;
  }
}

function renderSingleResults(results, baselineResult, currency, top1) {
  const html = results.map((r, idx) => {
    const isTop1 = r === top1;
    const isBaseline = r.strategy.isBaseline;
    const diff = baselineResult ? r.returnPct - baselineResult.returnPct : 0;
    const sub = isBaseline
      ? `연 ${fmtPct(r.annualizedPct)}`
      : (baselineResult ? `월급날 대비 ${fmtPct(diff, true)}p` : "");
    return `
      <div class="result-row ${isTop1 ? "top1" : ""}">
        <div class="r-rank">${isTop1 ? "🥇" : idx + 1}</div>
        <div class="r-main">
          <div class="r-label-row">
            <span>${escapeHtml(r.strategy.label)}</span>
            ${isBaseline ? '<span class="r-badge">📅 월급날 기준</span>' : ""}
          </div>
          <div class="r-invested">납입 ${fmtMoney(r.invested, currency)} → ${fmtMoney(r.finalValue, currency)}</div>
        </div>
        <div class="r-figures">
          <div class="r-pct ${r.returnPct < 0 ? "neg" : ""}">${fmtPct(r.returnPct)}</div>
          <div class="r-sub">${sub}</div>
        </div>
      </div>`;
  }).join("");
  $("singleResultsList").innerHTML = html;
}

/* ============================================================
   Chart
   ============================================================ */
function renderChart(topResult, baseResult, rows, currency) {
  const svg = $("chartSvg");
  const legend = $("chartLegend");
  const tooltip = $("chartTooltip");
  const sameStrategy = topResult.strategy.id === baseResult.strategy.id;

  legend.innerHTML = `
    <span class="lg-item"><span class="lg-swatch" style="background:var(--series-1);"></span>${escapeHtml(topResult.strategy.label)}</span>
    ${!sameStrategy ? `<span class="lg-item"><span class="lg-swatch" style="background:var(--text-muted); background-image:repeating-linear-gradient(90deg,var(--text-muted) 0 4px,transparent 4px 8px);"></span>${escapeHtml(baseResult.strategy.label)}</span>` : ""}
  `;

  const W = 640, H = 260, padL = 42, padR = 12, padT = 16, padB = 26;
  const n = rows.length;
  const dates = rows.map((r) => isoToDate(r.date).getTime());
  const minX = dates[0], maxX = dates[n - 1];

  const seriesList = sameStrategy ? [topResult.curve] : [topResult.curve, baseResult.curve];
  let minY = 0, maxY = 0;
  seriesList.forEach((c) => c.forEach((p) => { if (p.returnPct < minY) minY = p.returnPct; if (p.returnPct > maxY) maxY = p.returnPct; }));
  const yPad = Math.max(2, (maxY - minY) * 0.12);
  minY -= yPad; maxY += yPad;
  if (maxY - minY < 4) { maxY += 2; minY -= 2; }

  const xScale = (t) => padL + ((t - minX) / (maxX - minX || 1)) * (W - padL - padR);
  const yScale = (v) => padT + (1 - (v - minY) / (maxY - minY || 1)) * (H - padT - padB);

  const yTicks = 4;
  let gridSvg = "";
  for (let i = 0; i <= yTicks; i++) {
    const v = minY + ((maxY - minY) * i) / yTicks;
    const y = yScale(v);
    gridSvg += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--grid-line)" stroke-width="1" />`;
    gridSvg += `<text x="${padL - 6}" y="${y + 3}" text-anchor="end" font-size="9.5" fill="var(--text-muted)">${v.toFixed(0)}%</text>`;
  }
  const zeroY = yScale(0);
  gridSvg += `<line x1="${padL}" y1="${zeroY}" x2="${W - padR}" y2="${zeroY}" stroke="var(--axis-line)" stroke-width="1.2" />`;

  const xTickCount = 4;
  let xAxisSvg = "";
  for (let i = 0; i <= xTickCount; i++) {
    const t = minX + ((maxX - minX) * i) / xTickCount;
    const x = xScale(t);
    const label = new Date(t).toISOString().slice(0, 7);
    xAxisSvg += `<text x="${x}" y="${H - 6}" text-anchor="middle" font-size="9.5" fill="var(--text-muted)">${label}</text>`;
  }

  const pathFor = (curve) => curve.map((p, i) => `${i === 0 ? "M" : "L"}${xScale(dates[i]).toFixed(1)},${yScale(p.returnPct).toFixed(1)}`).join(" ");

  let linesSvg = "";
  if (!sameStrategy) {
    linesSvg += `<path d="${pathFor(baseResult.curve)}" fill="none" stroke="var(--text-muted)" stroke-width="2" stroke-dasharray="4 4" stroke-linecap="round" />`;
  }
  linesSvg += `<path d="${pathFor(topResult.curve)}" fill="none" stroke="var(--series-1)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />`;

  svg.innerHTML = `
    <g>${gridSvg}${xAxisSvg}${linesSvg}</g>
    <g id="hoverLayer" style="display:none;">
      <line id="crosshair" x1="0" y1="${padT}" x2="0" y2="${H - padB}" stroke="var(--axis-line)" stroke-width="1" stroke-dasharray="3 3" />
      <circle id="dotTop" r="4" fill="var(--series-1)" stroke="#fff" stroke-width="1.5" />
      <circle id="dotBase" r="4" fill="var(--text-muted)" stroke="#fff" stroke-width="1.5" style="display:${sameStrategy ? "none" : "block"};" />
    </g>
    <rect id="hoverCapture" x="${padL}" y="${padT}" width="${W - padL - padR}" height="${H - padT - padB}" fill="transparent" />
  `;

  const capture = $("hoverCapture");
  const hoverLayer = $("hoverLayer");
  const crosshair = $("crosshair");
  const dotTop = $("dotTop");
  const dotBase = $("dotBase");
  const wrap = svg.parentElement;

  function handleMove(clientX, clientY) {
    const pt = svg.createSVGPoint();
    pt.x = clientX; pt.y = clientY;
    const svgP = pt.matrixTransform(svg.getScreenCTM().inverse());
    const ratio = Math.min(1, Math.max(0, (svgP.x - padL) / (W - padL - padR)));
    const idx = Math.round(ratio * (n - 1));
    const x = xScale(dates[idx]);
    crosshair.setAttribute("x1", x); crosshair.setAttribute("x2", x);
    dotTop.setAttribute("cx", x); dotTop.setAttribute("cy", yScale(topResult.curve[idx].returnPct));
    if (!sameStrategy) { dotBase.setAttribute("cx", x); dotBase.setAttribute("cy", yScale(baseResult.curve[idx].returnPct)); }
    hoverLayer.style.display = "block";

    const rect = wrap.getBoundingClientRect();
    const px = (x / W) * rect.width;
    tooltip.style.left = `${px}px`;
    tooltip.style.top = `${(yScale(topResult.curve[idx].returnPct) / H) * rect.height}px`;
    tooltip.style.display = "block";
    tooltip.innerHTML = `
      <div class="tt-date">${rows[idx].date}</div>
      <div class="tt-row"><span class="lg-swatch" style="background:var(--series-1);"></span>${fmtPct(topResult.curve[idx].returnPct)}</div>
      ${!sameStrategy ? `<div class="tt-row"><span class="lg-swatch" style="background:var(--text-muted);"></span>${fmtPct(baseResult.curve[idx].returnPct)}</div>` : ""}
    `;
  }
  function hide() { hoverLayer.style.display = "none"; tooltip.style.display = "none"; }

  capture.addEventListener("mousemove", (e) => handleMove(e.clientX, e.clientY));
  capture.addEventListener("mouseleave", hide);
  capture.addEventListener("touchstart", (e) => { const t = e.touches[0]; handleMove(t.clientX, t.clientY); }, { passive: true });
  capture.addEventListener("touchmove", (e) => { const t = e.touches[0]; handleMove(t.clientX, t.clientY); }, { passive: true });
  capture.addEventListener("touchend", hide);

  // accessible table view: monthly snapshots
  const table = $("chartTable");
  const monthGroups = buildMonthGroups(rows);
  const stepIdxs = monthGroups.map((g) => g.indices[g.indices.length - 1]);
  let rowsHtml = stepIdxs.map((i) => `
    <tr>
      <td>${rows[i].date}</td>
      <td>${fmtPct(topResult.curve[i].returnPct)}</td>
      ${!sameStrategy ? `<td>${fmtPct(baseResult.curve[i].returnPct)}</td>` : ""}
    </tr>`).join("");
  table.innerHTML = `
    <thead><tr><th>월말 기준일</th><th>${escapeHtml(topResult.strategy.label)}</th>${!sameStrategy ? `<th>${escapeHtml(baseResult.strategy.label)}</th>` : ""}</tr></thead>
    <tbody>${rowsHtml}</tbody>`;
}

/* ============================================================
   Share link
   ============================================================ */
function updateShareUrl() {
  if (!state.ticker) return;
  const s = getSettings();
  const params = new URLSearchParams({
    symbol: state.ticker.symbol,
    name: state.ticker.name,
    start: s.start, end: s.end,
    budget: s.monthlyBudget, payday: s.payday, fee: s.feePct,
    ids: [...state.enabledIds].join(","),
  });
  history.replaceState(null, "", `?${params.toString()}`);
}

function applyUrlParams() {
  const p = new URLSearchParams(location.search);
  if (!p.get("symbol")) return false;
  state.ticker = { symbol: p.get("symbol"), name: p.get("name") || p.get("symbol"), currency: /\.(ks|kq)$/i.test(p.get("symbol")) ? "KRW" : "USD" };
  if (p.get("start")) $("startDate").value = p.get("start");
  if (p.get("end")) $("endDate").value = p.get("end");
  if (p.get("budget")) $("monthlyBudget").value = p.get("budget");
  if (p.get("payday")) $("payday").value = p.get("payday");
  if (p.get("fee")) $("feePct").value = p.get("fee");
  if (p.get("ids")) {
    state.enabledIds = new Set(p.get("ids").split(",").filter(Boolean));
  }
  return true;
}

/* ============================================================
   Tab 2: multi ticker
   ============================================================ */
function renderWatchlist() {
  $("watchlist").innerHTML = state.watchlist.map((t, idx) => `
    <span class="chip">${escapeHtml(t.symbol)}<button type="button" data-idx="${idx}">✕</button></span>
  `).join("");
  $("watchlist").querySelectorAll("button[data-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.watchlist.splice(parseInt(btn.dataset.idx, 10), 1);
      renderWatchlist();
    });
  });
}

async function runMultiBacktest() {
  if (!state.watchlist.length) { alert("비교할 종목을 먼저 추가해주세요."); return; }
  const enabled = getEnabledStrategies();
  if (!enabled.length) { alert("[단일 종목 분석] 탭에서 최소 1개 이상의 전략을 선택해주세요."); return; }

  const settings = getSettings();
  const card = $("multiResultsCard");
  card.style.display = "block";
  $("multiResultsList").innerHTML = "";
  $("runMultiBtn").disabled = true;

  const rowsOut = [];
  for (let i = 0; i < state.watchlist.length; i++) {
    const t = state.watchlist[i];
    $("multiStatus").innerHTML = `<div class="status-msg"><span class="spinner" style="border-color:rgba(0,0,0,.15); border-top-color:var(--series-1);"></span>${i + 1}/${state.watchlist.length} · ${escapeHtml(t.symbol)} 분석 중...</div>`;
    try {
      const data = await apiHistory(t.symbol, settings.start, settings.end);
      const ctx = buildCtx(data.rows);
      const results = enabled.map((s) => simulateStrategy(ctx, s, settings.monthlyBudget, settings.feePct, false));
      results.sort((a, b) => b.returnPct - a.returnPct);
      const baselineStrategy = getAllStrategies().find((s) => s.isBaseline);
      const baselineResult = baselineStrategy
        ? (results.find((r) => r.strategy.id === baselineStrategy.id) || simulateStrategy(ctx, baselineStrategy, settings.monthlyBudget, settings.feePct, false))
        : null;
      const best = results[0];
      rowsOut.push({
        symbol: t.symbol, name: data.name || t.symbol, currency: data.currency,
        best, diff: baselineResult ? best.returnPct - baselineResult.returnPct : null,
      });
    } catch (e) {
      rowsOut.push({ symbol: t.symbol, name: t.symbol, error: e.message || String(e) });
    }
  }

  rowsOut.sort((a, b) => (b.best ? b.best.returnPct : -999) - (a.best ? a.best.returnPct : -999));
  $("multiStatus").innerHTML = "";
  $("multiResultsList").innerHTML = rowsOut.map((r, idx) => {
    if (r.error) {
      return `<div class="m-row" style="cursor:default;"><div class="m-rank">-</div><div class="m-main"><div class="m-symbol">${escapeHtml(r.symbol)}</div><div class="m-strategy" style="color:var(--bad-text);">⚠️ ${escapeHtml(r.error)}</div></div></div>`;
    }
    return `
      <div class="m-row" data-symbol="${escapeHtml(r.symbol)}" data-name="${escapeHtml(r.name)}">
        <div class="m-rank">${idx + 1}</div>
        <div class="m-main">
          <div class="m-symbol">${escapeHtml(r.name)} <span style="color:var(--text-muted); font-weight:500;">(${escapeHtml(r.symbol)})</span></div>
          <div class="m-strategy">🏆 최적: ${escapeHtml(r.best.strategy.label)}</div>
        </div>
        <div>
          <div class="m-pct ${r.best.returnPct < 0 ? "neg" : ""}">${fmtPct(r.best.returnPct)}</div>
          <div class="m-ann">연 ${fmtPct(r.best.annualizedPct)}</div>
        </div>
      </div>`;
  }).join("");

  $("multiResultsList").querySelectorAll(".m-row[data-symbol]").forEach((el) => {
    el.addEventListener("click", () => {
      state.ticker = { symbol: el.dataset.symbol, name: el.dataset.name, currency: /\.(ks|kq)$/i.test(el.dataset.symbol) ? "KRW" : "USD" };
      switchTab("single");
      renderSelectedTicker();
      runSingleBacktest();
    });
  });

  $("runMultiBtn").disabled = false;
}

/* ============================================================
   Tabs
   ============================================================ */
function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tabpanel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ============================================================
   Init
   ============================================================ */
function init() {
  $("endDate").value = todayStr();
  $("startDate").value = yearsAgoStr(5);

  const cameFromShare = applyUrlParams();
  if (!state.enabledIds.size) initDefaultEnabled();

  renderStrategyList();
  wirePeriodPresets();
  wireCustomBuilder();
  renderSelectedTicker();
  renderWatchlist();

  document.querySelectorAll(".tab-btn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  attachTickerSearch($("tickerInput"), $("tickerResults"), (t) => {
    state.ticker = t;
    renderSelectedTicker();
  });
  attachTickerSearch($("tickerInputMulti"), $("tickerResultsMulti"), (t) => {
    if (!state.watchlist.some((w) => w.symbol === t.symbol)) state.watchlist.push(t);
    renderWatchlist();
  });

  $("runSingleBtn").addEventListener("click", runSingleBacktest);
  $("runMultiBtn").addEventListener("click", runMultiBacktest);

  $("shareLinkBtn").addEventListener("click", async () => {
    updateShareUrl();
    try {
      await navigator.clipboard.writeText(location.href);
      $("shareLinkBtn").textContent = "✅ 링크가 복사되었습니다";
      setTimeout(() => { $("shareLinkBtn").textContent = "🔗 이 결과 링크 복사"; }, 1800);
    } catch {
      prompt("아래 링크를 복사하세요:", location.href);
    }
  });

  if (cameFromShare) runSingleBacktest();
}

document.addEventListener("DOMContentLoaded", init);
