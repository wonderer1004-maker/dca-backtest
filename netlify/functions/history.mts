// Fetches daily historical prices from Yahoo Finance's chart API, server-side
// (Yahoo does not send CORS headers so this cannot be called from the browser
// directly). Falls back to Stooq's CSV export if Yahoo fails or returns no rows.
export default async (req) => {
  const url = new URL(req.url);
  const symbol = (url.searchParams.get("symbol") || "").trim();
  const start = url.searchParams.get("start"); // YYYY-MM-DD
  const end = url.searchParams.get("end"); // YYYY-MM-DD

  if (!symbol) {
    return new Response(JSON.stringify({ error: "symbol is required" }), {
      status: 400,
      headers: { "content-type": "application/json" },
    });
  }

  const period1 = start ? Math.floor(new Date(`${start}T00:00:00Z`).getTime() / 1000) : 0;
  const period2 = end
    ? Math.floor(new Date(`${end}T23:59:59Z`).getTime() / 1000)
    : Math.floor(Date.now() / 1000);

  try {
    const rows = await fetchFromYahoo(symbol, period1, period2);
    if (rows.length > 0) {
      const meta = await fetchYahooMeta(symbol);
      return json({ symbol, rows, ...meta });
    }
    throw new Error("yahoo returned no rows");
  } catch (e) {
    try {
      const rows = await fetchFromStooq(symbol, start, end);
      if (rows.length > 0) {
        return json({ symbol, rows, currency: guessCurrency(symbol), name: symbol });
      }
      throw new Error("stooq returned no rows");
    } catch (e2) {
      return new Response(
        JSON.stringify({
          error: `가격 데이터를 가져오지 못했습니다 (${symbol}). 종목 코드를 확인해주세요.`,
          detail: String(e2?.message || e2),
        }),
        { status: 502, headers: { "content-type": "application/json" } }
      );
    }
  }
};

async function fetchFromYahoo(symbol, period1, period2) {
  const yUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(
    symbol
  )}?period1=${period1}&period2=${period2}&interval=1d&events=div,splits`;

  const r = await fetch(yUrl, {
    headers: {
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      Accept: "application/json",
    },
  });
  if (!r.ok) throw new Error(`yahoo chart ${r.status}`);
  const data = await r.json();
  const result = data?.chart?.result?.[0];
  if (!result) {
    const desc = data?.chart?.error?.description;
    throw new Error(desc || "no chart result");
  }

  const ts = result.timestamp || [];
  const quote = result.indicators?.quote?.[0] || {};
  const adjArr = result.indicators?.adjclose?.[0]?.adjclose || quote.close || [];

  const rows = [];
  for (let i = 0; i < ts.length; i++) {
    const close = quote.close?.[i];
    const adjclose = adjArr?.[i];
    if (close == null || adjclose == null) continue;
    rows.push({
      date: new Date(ts[i] * 1000).toISOString().slice(0, 10),
      close,
      adjclose,
    });
  }
  return rows;
}

async function fetchYahooMeta(symbol) {
  try {
    const yUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(
      symbol
    )}?range=5d&interval=1d`;
    const r = await fetch(yUrl, {
      headers: { "User-Agent": "Mozilla/5.0", Accept: "application/json" },
    });
    const data = await r.json();
    const meta = data?.chart?.result?.[0]?.meta;
    return {
      currency: meta?.currency || guessCurrency(symbol),
      name: meta?.longName || meta?.shortName || symbol,
    };
  } catch {
    return { currency: guessCurrency(symbol), name: symbol };
  }
}

async function fetchFromStooq(symbol, start, end) {
  // Stooq expects lowercase symbols with a market suffix, e.g. aapl.us
  let s = symbol.toLowerCase();
  if (!s.includes(".")) s = `${s}.us`;
  else if (s.endsWith(".ks") || s.endsWith(".kq")) s = s.replace(/\.(ks|kq)$/, ".kr");

  const params = new URLSearchParams({ s, i: "d" });
  if (start) params.set("d1", start.replace(/-/g, ""));
  if (end) params.set("d2", end.replace(/-/g, ""));

  const csvUrl = `https://stooq.com/q/d/l/?${params.toString()}`;
  const r = await fetch(csvUrl, { headers: { "User-Agent": "Mozilla/5.0" } });
  if (!r.ok) throw new Error(`stooq ${r.status}`);
  const text = await r.text();
  if (!text || text.startsWith("<") || text.toLowerCase().includes("exceeded")) {
    throw new Error("stooq: no csv data");
  }

  const lines = text.trim().split("\n");
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(",");
    if (cols.length < 5) continue;
    const [date, , , , close] = cols;
    const c = parseFloat(close);
    if (!date || Number.isNaN(c)) continue;
    rows.push({ date, close: c, adjclose: c });
  }
  return rows;
}

function guessCurrency(symbol) {
  return /\.(ks|kq)$/i.test(symbol) ? "KRW" : "USD";
}

function json(body) {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

export const config = {
  path: "/api/history",
};
