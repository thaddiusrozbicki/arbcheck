// ArbCheck website: fetches live data from the API and fills in the page.
// Every address is relative (/api/...), so the same files work on localhost and on the VPS.

const MAX_AGE = 5; // seconds. Matches the logger: older prices never produce a trade plan.

const STATUS = {
  trade_plan_ready: ["ready", "Trade plan ready"],
  below_minimum: ["below", "Below 1¢ minimum"],
  fees_wipe_it_out: ["wiped", "Fees wipe it out"],
  no_gap: ["nogap", "No gap"],
  stale: ["stale", "Stale price"],
  not_verified: ["review", "Rules not verified"],
  no_data: ["stale", "Waiting for data"],
};

const RESULT = {
  tradable: ["ready", "Tradable"],
  below_minimum: ["below", "Below 1¢ minimum"],
  fees_wiped_it_out: ["wiped", "Fees wiped it out"],
};

// ---------- small helpers ----------

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url} returned ${res.status}`);
  return res.json();
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function cents(dollars, digits = 1) {
  if (dollars == null) return "None";
  const c = dollars * 100;
  // Tiny edges like 0.01¢ would round to 0.0¢, so show an extra digit for them
  return `${c.toFixed(c !== 0 && Math.abs(c) < 0.1 ? 2 : digits)}¢`;
}

function signedCents(dollars) {
  if (dollars == null) return '<td class="num muted">None</td>';
  const cls = dollars > 0 ? "pos" : "neg";
  const sign = dollars > 0 ? "+" : "−";
  return `<td class="num ${cls}">${sign}${cents(Math.abs(dollars))}</td>`;
}

function capitalize(text) {
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

function money(dollars) {
  return dollars == null ? "None" : `$${Number(dollars).toFixed(2)}`;
}

function etTime(iso, withDate = true, withSeconds = true) {
  if (!iso) return "";
  const opts = { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" };
  if (withSeconds) opts.second = "2-digit";
  if (withDate) Object.assign(opts, { month: "short", day: "numeric" });
  return new Date(iso).toLocaleString("en-US", opts);
}

function etDate(isoDate) {
  if (!isoDate) return "no date";
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 12)).toLocaleDateString("en-US",
    { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

function statusHTML(status) {
  const [cls, label] = STATUS[status] || ["stale", status];
  return `<span class="status ${cls}">${label}</span>`;
}

function ageHTML(iso) {
  return iso ? `<span class="age" data-ts="${esc(iso)}"></span>` : '<span class="age">no data</span>';
}

// Ages count up every second between refreshes, so an old price visibly gets older.
function tickAges() {
  const now = Date.now();
  document.querySelectorAll("[data-ts]").forEach((el) => {
    const s = Math.max(0, Math.round((now - Date.parse(el.dataset.ts)) / 1000));
    el.textContent = s < 120 ? `${s}s old` : `${Math.round(s / 60)}m old`;
    el.classList.toggle("is-stale", s > MAX_AGE);
  });
  document.querySelectorAll("[data-since]").forEach((el) => {
    const s = Math.max(0, Math.round((now - Date.parse(el.dataset.since)) / 1000));
    el.textContent = s < 120 ? `${s} seconds ago` : `${Math.round(s / 60)} minutes ago`;
  });
}

// The $1.00 bar. The track covers 94¢ to 104¢, so 1¢ is 10% and $1.00 sits at 60%.
function meterHTML(best) {
  const pos = (c) => Math.max(0, Math.min(100, (c - 94) * 10));
  if (!best || best.cost == null) return '<div class="meter is-stale"><span class="dollar"></span></div>';
  const cost = best.cost * 100;
  const parts = [`<span class="cost" style="width:${pos(cost).toFixed(1)}%"></span>`];
  let total = cost;
  if (best.fee != null && !best.stale) {
    const fee = best.fee * 100;
    parts.push(`<span class="fee" style="left:${pos(cost).toFixed(1)}%;width:${(pos(cost + fee) - pos(cost)).toFixed(1)}%"></span>`);
    total = cost + fee;
  }
  if (!best.stale) {
    if (total < 100) parts.push(`<span class="gap" style="left:${pos(total).toFixed(1)}%;width:${(60 - pos(total)).toFixed(1)}%"></span>`);
    else if (total > 100) parts.push(`<span class="over" style="left:60%;width:${(pos(total) - 60).toFixed(1)}%"></span>`);
  }
  parts.push('<span class="dollar"></span>');
  const label = `YES plus NO costs ${cost.toFixed(1)} cents`;
  return `<div class="meter${best.stale ? " is-stale" : ""}" role="img" aria-label="${label}">${parts.join("")}</div>`;
}

// ---------- header: is the poller alive? ----------

async function updateFeed() {
  const el = document.getElementById("feed");
  if (!el) return;
  try {
    const h = await getJSON("/api/health");
    if (!h.last_book_at) {
      el.innerHTML = "<strong>Waiting for the poller</strong><br>No prices saved yet";
    } else if (h.seconds_since_last_book > 60) {
      el.innerHTML = `<strong class="neg">Poller looks stopped</strong><br>Last poll <span data-since="${esc(h.last_book_at)}"></span>`;
    } else {
      el.innerHTML = `<strong>Live data</strong><br>Last poll <span data-since="${esc(h.last_book_at)}"></span>`;
    }
  } catch (err) {
    el.innerHTML = '<strong class="neg">Can\'t reach the API</strong><br>Is uvicorn running?';
  }
  tickAges();
}

// ---------- Pairs page ----------

async function renderPairs() {
  const body = document.getElementById("pairs-body");
  let pairs;
  try {
    pairs = await getJSON("/api/pairs");
  } catch (err) {
    body.innerHTML = '<tr><td colspan="6" class="muted">Can\'t load pairs from the API.</td></tr>';
    return;
  }
  if (!pairs.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">No active pairs. Add some to pairs/pairs.json.</td></tr>';
    return;
  }
  body.innerHTML = pairs.map((p) => {
    const b = p.best;
    const kalshi = b && b.kalshi_price != null
      ? `<span class="price k">${b.kalshi_side} ${cents(b.kalshi_price, 0)}</span>${ageHTML(b.kalshi_fetched_at)}`
      : `<span class="muted">No data</span>${ageHTML(b && b.kalshi_fetched_at)}`;
    const poly = b && b.polymarket_price != null
      ? `<span class="price p">${b.polymarket_side} ${cents(b.polymarket_price, 0)}</span>${ageHTML(b.polymarket_fetched_at)}`
      : `<span class="muted">No data</span>${ageHTML(b && b.polymarket_fetched_at)}`;
    const edge = b && !b.stale && b.status !== "no_gap" ? signedCents(b.net_edge) : '<td class="num muted">None</td>';
    return `
      <tr>
        <td><span class="pair-name"><a href="pair.html?id=${encodeURIComponent(p.id)}">${esc(p.name)}</a></span>
            <span class="pair-meta">${esc(capitalize(p.category))}, resolves ${etDate(p.resolution_date)}</span></td>
        <td>${kalshi}</td>
        <td>${poly}</td>
        <td>${meterHTML(b)}</td>
        ${edge}
        <td>${statusHTML(p.status)}</td>
      </tr>`;
  }).join("");

  const ready = pairs.filter((p) => p.status === "trade_plan_ready").length;
  const stale = pairs.filter((p) => p.status === "stale").length;
  const waiting = pairs.filter((p) => p.status === "no_data").length;
  const parts = [`${pairs.length} active pairs`, `${ready} with a trade plan right now`];
  if (stale) parts.push(`${stale} with a stale price`);
  if (waiting) parts.push(`${waiting} waiting for data`);
  document.getElementById("summary").textContent = parts.join(", ") + ".";
  tickAges();
}

// ---------- Pair detail page ----------

const NO_PLAN_REASON = {
  stale: "One of the prices is more than 5 seconds old, so it can't be trusted.",
  no_gap: "YES plus NO costs at least $1.00, so there's nothing to capture.",
  fees_wipe_it_out: "There's a gap, but fees are bigger than it.",
  below_minimum: "There's a gap, but it's under the 1¢ minimum after fees.",
  not_verified: "An admin hasn't verified that this pair's rules match.",
  no_data: "Waiting for prices from both exchanges.",
  trade_plan_ready: "No size makes money after fees right now.",
};

function fillsText(leg) {
  const parts = leg.fills.map(([price, size]) => `${Number(size).toLocaleString()} at ${cents(price, 1)}`);
  return `${parts.join(" and ")}, for ${money(leg.cost)}. Price ${Math.round(leg.price_age_seconds)}s old.`;
}

function bookHTML(book, plan) {
  const isKalshi = book.exchange === "kalshi";
  const used = new Set();
  if (plan) {
    plan.legs.filter((l) => l.exchange === book.exchange).forEach((l) => l.fills.forEach(([p]) => used.add(p)));
  }
  const rows = book.asks.length
    ? book.asks.map(([price, size]) =>
        `<tr${used.has(price) ? ' class="used"' : ""}><td>${cents(price, 1)}</td><td class="num">${Number(size).toLocaleString()}</td></tr>`).join("")
    : '<tr><td colspan="2" class="muted">No asks</td></tr>';
  const note = isKalshi
    ? "Kalshi only publishes bids, so each ask is $1.00 minus the other side's bid."
    : `Read straight from the ${book.outcome} token's book.`;
  return `
    <div class="book">
      <h3 class="${isKalshi ? "k" : "p"}">${isKalshi ? "Kalshi" : "Polymarket"} ${book.outcome} asks</h3>
      <p>${note} Fetched <span data-ts="${esc(book.fetched_at)}"></span>.</p>
      <div class="table-wrap"><table>
        <thead><tr><th scope="col">${book.outcome} ask</th><th scope="col" class="num">Size</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </div>`;
}

function planHTML(d) {
  const plan = d.trade_plan;
  if (!plan) {
    return `<h2 id="plan-h">Best trade plan</h2>
      <p>${statusHTML(d.status)}</p>
      <p class="muted">No trade plan right now. ${NO_PLAN_REASON[d.status] || ""}</p>`;
  }
  const legs = plan.legs.map((l) => `
    <li><span class="leg-title ${l.exchange === "kalshi" ? "k" : "p"}">Buy ${plan.quantity.toLocaleString()} ${l.outcome} on ${l.exchange === "kalshi" ? "Kalshi" : "Polymarket"}</span>
        <span class="leg-detail">${fillsText(l)}</span></li>`).join("");
  const fees = plan.legs.map((l) => `<dt>${l.exchange === "kalshi" ? "Kalshi" : "Polymarket"} fee</dt><dd>${money(l.fees)}</dd>`).join("");
  return `<h2 id="plan-h">Best trade plan</h2>
    <p class="small muted">${esc(d.best.direction.replace("kalshi", "Kalshi").replace("polymarket", "Polymarket"))}</p>
    <ul class="legs">${legs}</ul>
    <dl class="figures">
      <dt>Contracts</dt><dd>${money(plan.contracts_cost)}</dd>
      ${fees}
      <dt class="total">Total cost</dt><dd class="total">${money(plan.total_cost)}</dd>
      <dt>Payout either way</dt><dd>${money(plan.payout)}</dd>
      <dt>Profit</dt><dd class="profit">${money(plan.profit)}</dd>
      <dt>Return</dt><dd>${plan.return_pct.toFixed(2)}%</dd>
      <dt>Annualized return</dt><dd>${plan.annualized_pct == null ? "None" : plan.annualized_pct.toFixed(1) + "%"}</dd>
    </dl>`;
}

async function renderPair() {
  const id = new URLSearchParams(location.search).get("id");
  const main = document.getElementById("pair-main");
  if (!id) {
    main.innerHTML = '<p class="muted">Pick a pair from the <a href="index.html">Pairs page</a>.</p>';
    return;
  }
  let d;
  try {
    d = await getJSON(`/api/pairs/${encodeURIComponent(id)}`);
  } catch (err) {
    main.innerHTML = `<p class="muted">Couldn't load ${esc(id)}. <a href="index.html">Back to all pairs</a>.</p>`;
    return;
  }
  document.title = `${d.name} | ArbCheck`;
  document.getElementById("pair-title").textContent = d.name;
  document.getElementById("pair-meta").innerHTML = `
    <span>Resolves <b>${etDate(d.resolution_date)}</b>${d.days_to_resolution ? `, in ${d.days_to_resolution} days` : ""}</span>
    <span>Kalshi <b>${esc(d.kalshi_ticker || "")}</b></span>
    <span>${esc(capitalize(d.category))}</span>`;
  document.getElementById("plan").innerHTML = planHTML(d);
  document.getElementById("books").innerHTML = d.books.length
    ? d.books.map((b) => bookHTML(b, d.trade_plan)).join('<div style="height:20px"></div>') +
      (d.trade_plan ? '<p class="small muted">Highlighted levels are part of the trade plan.</p>' : "")
    : '<p class="muted">No order books yet for this pair.</p>';
  const links = [];
  if (d.kalshi_rules_url) links.push(`<a href="${esc(d.kalshi_rules_url)}" target="_blank" rel="noopener">Kalshi rules</a>`);
  if (d.poly_rules_url) links.push(`<a href="${esc(d.poly_rules_url)}" target="_blank" rel="noopener">Polymarket rules</a>`);
  document.getElementById("rules-links").innerHTML = links.join(" and ") || "No rule links saved.";
  document.getElementById("rules-notes").textContent = d.rule_notes || "No rule notes yet.";
  tickAges();
}

// ---------- Opportunities page ----------

function closedHTML(e) {
  if (e.end_reason === "open") return '<span class="open-now">Open now</span>';
  if (e.end_reason === "still_open") return '<span class="muted">Still open when the poller stopped</span>';
  if (e.end_reason === "stale") return '<span class="muted">Unknown, a feed went quiet</span>';
  return etTime(e.closed_at, false);
}

function directionHTML(direction) {
  const [first, second] = direction.split(" + ").map((side) => {
    const [ex, outcome] = side.split(" ");
    return `<span class="${ex === "kalshi" ? "k" : "p"}">${ex === "kalshi" ? "Kalshi" : "Polymarket"} ${outcome}</span>`;
  });
  return `${first}<br>${second}`;
}

async function renderOpportunities() {
  const body = document.getElementById("opps-body");
  let entries;
  try {
    entries = await getJSON("/api/opportunities?limit=100");
  } catch (err) {
    body.innerHTML = '<tr><td colspan="8" class="muted">Can\'t load the log from the API.</td></tr>';
    return;
  }
  if (!entries.length) {
    body.innerHTML = '<tr><td colspan="8" class="muted">No gaps logged yet. Real arbitrage is rare, so this can take a while.</td></tr>';
  } else {
    body.innerHTML = entries.map((e) => {
      const [cls, label] = RESULT[e.result] || ["stale", e.result];
      return `
        <tr${e.end_reason === "open" ? ' class="is-open"' : ""}>
          <td class="pair-name"><a href="pair.html?id=${encodeURIComponent(e.pair_id)}">${esc(e.name)}</a></td>
          <td class="nowrap">${directionHTML(e.direction)}</td>
          <td class="nowrap">${etTime(e.opened_at)}</td>
          <td class="nowrap">${closedHTML(e)}</td>
          <td class="num">${Number(e.max_size).toLocaleString()}</td>
          <td class="num">${cents(Number(e.best_gross_edge))}</td>
          ${signedCents(Number(e.best_net_edge))}
          <td><span class="status ${cls}">${label}</span></td>
        </tr>`;
    }).join("");
  }
  document.getElementById("opps-note").textContent =
    `Showing the ${entries.length} most recent. Times are Eastern. Updates every 10 seconds.`;
}

// ---------- Results page ----------

async function renderResults() {
  let r;
  try {
    r = await getJSON("/api/results");
  } catch (err) {
    document.getElementById("lead").textContent = "Can't load results from the API.";
    return;
  }
  const since = r.first_seen ? ` since ${etTime(r.first_seen, true, false)} ET` : "";
  document.getElementById("lead").textContent = r.gaps_before_fees
    ? `The scanner has logged ${r.gaps_before_fees} price gaps between Kalshi and Polymarket${since}. ` +
      `${r.survived_fees} were still positive after fees, and ${r.tradable} cleared the 1¢ minimum.`
    : "No gaps logged yet. Leave the poller running and check back.";
  const total = Math.max(1, r.gaps_before_fees);
  document.getElementById("bar-all").style.width = r.gaps_before_fees ? "100%" : "0%";
  document.getElementById("bar-survived").style.width = `${(100 * r.survived_fees / total).toFixed(1)}%`;
  document.getElementById("count-all").textContent = r.gaps_before_fees;
  document.getElementById("count-survived").textContent = r.survived_fees;
  document.getElementById("survivors-h").textContent = `How big the ${r.survived_fees} survivors were`;
  document.getElementById("stat-edge").textContent = r.median_net_edge == null ? "None" : cents(r.median_net_edge, 2);
  document.getElementById("stat-size").textContent = r.median_size == null ? "None" : `${Math.round(r.median_size).toLocaleString()} pairs`;
  document.getElementById("stat-profit").textContent = money(r.median_profit);
  document.getElementById("stat-largest").textContent = money(r.largest_profit);
  document.getElementById("fee-note").textContent =
    `Fees come from ${r.fee_model}. ${r.open_now} gap${r.open_now === 1 ? " is" : "s are"} open right now.`;
}

// ---------- start ----------

const PAGES = {
  pairs: [renderPairs, 5000],
  pair: [renderPair, 5000],
  opportunities: [renderOpportunities, 10000],
  results: [renderResults, 15000],
};

document.addEventListener("DOMContentLoaded", () => {
  updateFeed();
  setInterval(updateFeed, 5000);
  setInterval(tickAges, 1000);
  const page = PAGES[document.body.dataset.page];
  if (page) {
    const [render, every] = page;
    render();
    setInterval(render, every);
  }
});
