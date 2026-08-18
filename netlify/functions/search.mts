// Proxies Yahoo Finance's ticker search so the browser never has to hit
// a cross-origin endpoint directly (Yahoo does not send CORS headers).
export default async (req) => {
  const url = new URL(req.url);
  const q = (url.searchParams.get("q") || "").trim();

  if (!q) {
    return new Response(JSON.stringify({ results: [] }), {
      headers: { "content-type": "application/json" },
    });
  }

  try {
    const yUrl = `https://query2.finance.yahoo.com/v1/finance/search?q=${encodeURIComponent(
      q
    )}&quotesCount=10&newsCount=0&lang=ko-KR&region=KR`;

    const r = await fetch(yUrl, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        Accept: "application/json",
      },
    });

    if (!r.ok) throw new Error(`yahoo search ${r.status}`);
    const data = await r.json();

    const quotes = Array.isArray(data?.quotes) ? data.quotes : [];
    const results = quotes
      .filter((x) => x?.symbol && (x.quoteType === "EQUITY" || x.quoteType === "ETF"))
      .map((x) => ({
        symbol: x.symbol,
        name: x.shortname || x.longname || x.symbol,
        exchange: x.exchDisp || x.exchange || "",
        type: x.quoteType,
      }))
      .slice(0, 10);

    return new Response(JSON.stringify({ results }), {
      headers: { "content-type": "application/json" },
    });
  } catch (e) {
    return new Response(JSON.stringify({ results: [], error: String(e?.message || e) }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }
};

export const config = {
  path: "/api/search",
};
