const API = (window.BOT_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const WS = API.replace(/^http/, "ws");
const $ = (id) => document.getElementById(id);
const state = { dashboard: null, live: null, liveQuotes: null, liveReference: null, realtimeBtc: [], chart: { btc: [], market: [] }, health: null, rounds: [], trades: [], sessions: [], activeSessionSummary: null, selectedSessionId: null, sessionDetail: null, account: null, orderPreview: null, funds: null,
  pages: {
    history: { offset: 0, limit: 25, total: 0 },
    sessions: { offset: 0, limit: 8, total: 0 },
    trades: { offset: 0, limit: 25, total: 0 },
    events: { offset: 0, limit: 25, total: 0 },
    recovered: { offset: 0, limit: 20, total: 0 },
  },
};
const calculator = { side: "Up", amount: 50, presets: [1, 50, 100] };
let liveSocket, chartSocket, quoteSocket, referenceSocket, accountSocket, reconnectTimer;
let sessionPollInFlight = false;

const fmt = {
  percent: (value, digits = 1) => value == null ? "—" : `${(Number(value) * 100).toFixed(digits)}%`,
  usd: (value, digits = 2) => value == null ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value),
  price: (value) => value == null ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(value),
  num: (value, digits = 2) => value == null ? "—" : Number(value).toFixed(digits),
  cents: (value) => value == null ? "—" : `${Math.round(Number(value) * 100)}¢`,
  time: (seconds) => seconds == null ? "—" : `${Math.max(0, Math.floor(seconds / 60))}:${String(Math.max(0, Math.floor(seconds % 60))).padStart(2, "0")}`,
  date: (seconds) => seconds == null ? "—" : new Date(seconds * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
};

async function request(path, options) {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const body = await response.text();
    try { throw new Error(JSON.parse(body).detail || `Request failed (${response.status})`); }
    catch (error) { if (error instanceof SyntaxError) throw new Error(body || `Request failed (${response.status})`); throw error; }
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

function setConnection(mode, message) {
  $("connectionDot").className = mode;
  $("connectionText").textContent = message;
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 2400);
}

function badge(value) {
  const label = value || "PENDING";
  return `<span class="result ${label.toLowerCase()}">${label}</span>`;
}

function renderPagination(name) {
  const page = state.pages[name];
  const container = $(`${name}Pagination`);
  if (!page || !container) return;
  const pageNumber = page.total ? Math.floor(page.offset / page.limit) + 1 : 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  $(`${name}PageInfo`).textContent = `${pageNumber} / ${pageCount} · ${page.total} record${page.total === 1 ? "" : "s"}`;
  $(`${name}Previous`).disabled = page.offset <= 0;
  $(`${name}Next`).disabled = page.offset + page.limit >= page.total;
  container.classList.toggle("hidden", page.total === 0);
}

function movePage(name, direction, loader) {
  const page = state.pages[name];
  const next = page.offset + direction * page.limit;
  if (next < 0 || next >= page.total) return;
  page.offset = next;
  loader().catch(error => toast(error.message));
}

function sideClass(side) { return String(side || "neutral").toLowerCase(); }

function prepareMobileTables() {
  document.querySelectorAll(".table-scroll table").forEach(table => {
    const labels = [...table.querySelectorAll("thead th")].map(cell => cell.textContent.trim());
    table.querySelectorAll("tbody tr").forEach(row => {
      [...row.children].forEach((cell, index) => {
        cell.dataset.label = labels[index] || "Detail";
      });
    });
  });
}

function renderLive() {
  const live = state.live || {};
  const prediction = live.prediction || {};
  const trading = live.trading;
  const primary = prediction;
  const direction = primary.direction || "—";
  const pUp = primary.p_up == null ? null : Number(primary.p_up);
  const pDown = primary.p_down == null ? (pUp == null ? null : 1 - pUp) : Number(primary.p_down);
  const canTrade = direction === "Up" || direction === "Down";
  const isSkip = direction === "SKIP" || prediction.status === "SKIP";
  const recommendation = canTrade ? `TRADE ${direction.toUpperCase()}` : isSkip ? "SKIP" : "WAITING";
  const recommendationClass = canTrade ? direction.toLowerCase() : isSkip ? "skip" : "neutral";
  const risk = assessRisk(primary, canTrade);

  $("currentRound").textContent = prediction.slug?.replace("btc-updown-5m-", "#") || "—";
  $("countdown").dataset.remaining = prediction.remaining_seconds ?? "";
  $("countdown").dataset.updated = prediction.updated_ms ?? Date.now();
  $("countdown").textContent = fmt.time(prediction.remaining_seconds);
  $("bankroll").textContent = trading ? fmt.usd(trading.trading_capital ?? trading.available_bankroll) : "Not running";
  $("directionArrow").textContent = direction === "Up" ? "↗" : direction === "Down" ? "↘" : isSkip ? "×" : "·";
  $("upProbability").textContent = fmt.percent(pUp);
  $("downProbability").textContent = fmt.percent(pDown);
  $("upProbabilityBar").style.width = `${Math.max(0, Math.min(100, Number(pUp || 0) * 100))}%`;
  $("downProbabilityBar").style.width = `${Math.max(0, Math.min(100, Number(pDown || 0) * 100))}%`;
  $("recommendation").textContent = recommendation;
  $("recommendationBox").className = `recommendation ${recommendationClass}`;
  $("signalState").textContent = canTrade ? "SIGNAL READY" : isSkip ? "NO TRADE" : "ANALYZING";
  $("signalState").className = `pill ${canTrade ? "signal" : isSkip ? "warning" : "muted"}`;
  $("riskLevel").textContent = risk.level;
  $("riskBadge").className = `risk-badge ${risk.className}`;
  $("riskReasons").innerHTML = risk.reasons.map(reason => `<span class="risk-reason">${reason}</span>`).join("");
  $("signalMessage").textContent = humanReason(primary.reason, prediction.message);
  $("edge").textContent = fmt.percent(primary.estimated_edge);
  const correctedGap = primary.distance_source === "chainlink_twap_60s" ? primary.distance_to_beat : null;
  $("distance").textContent = correctedGap == null ? "—" : `${correctedGap >= 0 ? "+" : ""}${fmt.usd(correctedGap)}`;
  $("distance").className = correctedGap == null ? "" : correctedGap >= 0 ? "positive" : "negative";
  renderReferenceNumbers();
  renderMarketQuotes();
  $("marketAgreement").textContent = primary.agreement?.market || "—";
  $("warnings").innerHTML = (primary.warnings || []).map(w => `<span class="warning-chip">⚠ ${w.replaceAll("_", " ")}</span>`).join("");
  renderAgreement(primary.agreement || {}, direction);
  renderStability(prediction.market_stability || {});
  renderTradingControl();
}

function assessRisk(prediction, canTrade) {
  if (!canTrade) {
    return { level: prediction.direction === "SKIP" || prediction.status === "SKIP" ? "AVOID" : "—",
             className: "neutral", reasons: [] };
  }
  let score = 0;
  const reasons = [];
  const warnings = prediction.warnings || [];
  if (warnings.includes("movement_disagreement")) {
    score += 2;
    reasons.push("BTC movement disagrees with the selected side");
  }
  if (warnings.includes("thin_gap")) {
    score += 2;
    reasons.push("BTC is less than 5 bps from the opening price");
  }
  const stabilityValue = prediction.market_stability?.stability_score;
  const stability = stabilityValue == null ? null : Number(stabilityValue);
  if (stability != null && Number.isFinite(stability) && stability < .4) {
    score += 2;
    reasons.push("Price movement is choppy and unstable");
  } else if (stability != null && Number.isFinite(stability) && stability < .65) {
    score += 1;
    reasons.push("Market stability is only moderate");
  }
  const crossingValue = prediction.crossing_probability;
  const crossing = crossingValue == null ? null : Number(crossingValue);
  if (crossing != null && Number.isFinite(crossing) && crossing >= .05) {
    score += 2;
    reasons.push("Meaningful probability of crossing the opening price");
  } else if (crossing != null && Number.isFinite(crossing) && crossing >= .02) {
    score += 1;
    reasons.push("Opening-price crossing risk is present");
  }
  const edgeValue = prediction.estimated_edge;
  const edge = edgeValue == null ? null : Number(edgeValue);
  if (edge != null && Number.isFinite(edge) && edge < .04) {
    score += 1;
    reasons.push("Profit edge is close to the minimum allowed");
  }
  const flow = prediction.agreement?.order_flow;
  if ((flow === "Up" || flow === "Down") && flow !== prediction.direction) {
    score += 1;
    reasons.push("Order flow leans toward the opposite side");
  }
  if (!reasons.length) reasons.push("Core signals agree with low measured crossing risk");
  return score >= 3 ? { level: "HIGH", className: "high", reasons }
       : score >= 1 ? { level: "MEDIUM", className: "medium", reasons }
       : { level: "LOW", className: "low", reasons };
}

function humanReason(reason, fallback) {
  if (!reason) return fallback || "Collecting live inputs for the prediction window.";
  const explanations = {
    analytic_unvalidated_signal: "The probability, market direction, liquidity, and profit edge passed.",
    analytic_probability_below_98pct: "Confidence is below the fixed 98% signal threshold.",
    analytic_strength_below_threshold: "Signal strength is not above 2.5.",
    analytic_thin_gap: "BTC is too close to the opening line.",
    analytic_movement_disagreement: "Recent BTC movement disagrees with the predicted side.",
    confidence_below_threshold: "The forecast did not reach the session confidence threshold.",
    price_below_entry_floor: "The contract is not above the hard 80¢ entry floor.",
    "analytic_disagreement:market": "The settlement estimate and Polymarket direction disagree.",
    analytic_edge_below_threshold: "The available price is too expensive for the required profit margin.",
    analytic_liquidity_unavailable: "There is not enough reliable liquidity at the best price.",
    market_disagreement: "The predicted side disagrees with Polymarket's direction.",
    decision_window_missed: "A complete snapshot was not available inside the decision window.",
  };
  return explanations[reason] || reason.replaceAll("_", " ");
}

function renderAgreement(agreement, selectedDirection) {
  const items = [["Settlement estimate", agreement.settlement], ["BTC movement", agreement.movement], ["Polymarket", agreement.market], ["Order flow", agreement.order_flow]];
  $("agreementList").innerHTML = items.map(([name, value]) => `<div class="agreement-row"><span>${name}</span><b class="${sideClass(value)}">${value || "Unavailable"}</b></div>`).join("");
  const direction = selectedDirection || state.live?.prediction?.direction;
  const agreed = items.slice(0, 3).filter(([, value]) => value === direction).length;
  $("agreementCount").textContent = `${agreed} / 3`;
}

function renderStability(data) {
  const score = data.stability_score;
  $("regime").textContent = (data.regime || "UNKNOWN").replaceAll("_", " ").toUpperCase();
  $("stabilityScore").textContent = fmt.percent(score, 0);
  $("stabilityMeter").style.width = `${Number(score || 0) * 100}%`;
  $("stabilityCopy").textContent = score == null ? "Movement history is still building." : score > .66 ? "Price movement has been steady and directional." : score < .4 ? "Price is choppy with frequent reversals." : "Price movement is mixed; treat the direction with care.";
  $("range30").textContent = data.range_30_bps == null ? "—" : `${fmt.num(data.range_30_bps, 1)} bps`;
  $("range120").textContent = data.range_120_bps == null ? "—" : `${fmt.num(data.range_120_bps, 1)} bps`;
  $("reversals").textContent = fmt.percent(data.reversal_rate_30, 0);
}

function path(points, x, y) {
  return points.map((point, index) => `${index ? "L" : "M"}${x(point).toFixed(2)},${y(point).toFixed(2)}`).join(" ");
}

function renderCharts() {
  renderMarketQuotes();
  const recordedBtc = !state.liveReference?.slug || state.chart.slug === state.liveReference.slug
    ? (state.chart.btc || [])
    : [];
  const btcSource = [...recordedBtc, ...state.realtimeBtc];
  const btc = btcSource
    .map((point, index) => ({
      index,
      timestamp: Number(point.timestamp_ms),
      price: Number(point.price ?? point.twap),
      opening: Number(point.opening_price),
    }))
    .filter(point => Number.isFinite(point.price) && point.price > 0)
    .sort((a, b) => (a.timestamp || a.index) - (b.timestamp || b.index));
  renderReferenceNumbers();
  const svg = $("priceChart");
  $("chartEmpty").classList.toggle("hidden", btc.length > 1);
  if (btc.length < 2) { svg.innerHTML = ""; return; }
  const width = 1000, height = 300, pad = { l: 12, r: 78, t: 24, b: 28 };
  const liveOpening = Number(state.liveReference?.opening_price);
  const pointOpening = btc.find(point => Number.isFinite(point.opening) && point.opening > 0)?.opening;
  const opening = Number.isFinite(liveOpening) && liveOpening > 0 ? liveOpening : pointOpening;
  const all = btc.map(point => point.price);
  if (Number.isFinite(opening)) all.push(opening);
  let min = Math.min(...all), max = Math.max(...all);
  const range = Math.max(max - min, 1);
  const margin = Math.max(range * .22, 1.5);
  min -= margin;
  max += margin;
  const timestampsValid = btc.every(point => Number.isFinite(point.timestamp));
  const firstTime = btc[0].timestamp;
  const lastTime = btc.at(-1).timestamp;
  const timeRange = lastTime - firstTime;
  const x = point => pad.l + (timestampsValid && timeRange > 0
    ? (point.timestamp - firstTime) / timeRange
    : point.index / (btc.length - 1)) * (width - pad.l - pad.r);
  const yValue = value => pad.t + (max - value) / (max - min) * (height - pad.t - pad.b);
  const pricePath = path(btc, x, point => yValue(point.price));
  const last = btc.at(-1);
  const lastX = x(last);
  const lastY = yValue(last.price);
  const openY = Number.isFinite(opening) ? yValue(opening) : null;
  const areaPath = `${pricePath} L${lastX.toFixed(2)},${height-pad.b} L${x(btc[0]).toFixed(2)},${height-pad.b} Z`;
  const grid = [0, .25, .5, .75, 1].map(ratio => {
    const y = pad.t + ratio * (height-pad.t-pad.b);
    const value = max-ratio*(max-min);
    return `<line class="grid-line" x1="${pad.l}" y1="${y}" x2="${width-pad.r}" y2="${y}"/><text class="axis-label" x="${width-pad.r+9}" y="${y+3}">$${value.toFixed(0)}</text>`;
  }).join("");
  const openGuide = openY == null ? "" : `<line class="line-open-glow" x1="${pad.l}" x2="${width-pad.r}" y1="${openY}" y2="${openY}"/><line class="line-open" x1="${pad.l}" x2="${width-pad.r}" y1="${openY}" y2="${openY}"/><text class="open-label" x="${pad.l+8}" y="${Math.max(pad.t+11, openY-8)}">OPEN  ${fmt.price(opening)}</text>`;
  const currentLabelY = Math.max(pad.t + 12, Math.min(height - pad.b - 8, lastY - 10));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `<defs><linearGradient id="priceArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#65efad" stop-opacity=".24"/><stop offset="100%" stop-color="#65efad" stop-opacity="0"/></linearGradient><filter id="priceGlow" x="-20%" y="-40%" width="140%" height="180%"><feGaussianBlur stdDeviation="3" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>${grid}<path class="price-area" d="${areaPath}"/>${openGuide}<path class="line-price-glow" d="${pricePath}"/><path class="line-price" d="${pricePath}"/><circle class="chart-dot-ring" cx="${lastX}" cy="${lastY}" r="8"/><circle class="chart-dot" cx="${lastX}" cy="${lastY}" r="4"/><text class="current-label" x="${lastX-10}" y="${currentLabelY}" text-anchor="end">NOW  ${fmt.price(last.price)}</text>`;
}

function renderReferenceNumbers() {
  const reference = state.liveReference || {};
  const latestChart = state.chart.btc?.at(-1) || {};
  const opening = Number(state.liveReference ? reference.opening_price : latestChart.opening_price);
  const current = Number(state.liveReference ? reference.current_price : latestChart.twap);
  const validOpen = Number.isFinite(opening) && opening > 0;
  const validCurrent = Number.isFinite(current) && current > 0;
  $("openingPrice").textContent = validOpen ? fmt.price(opening) : "Waiting for boundary price…";
  $("openingPrice").title = reference.opening_source === "gamma_metadata" ? "Official Polymarket market metadata" : "Exact Chainlink 60-second TWAP boundary tick";
  $("currentPrice").textContent = validCurrent ? fmt.price(current) : "Waiting for live price…";
  $("currentPrice").title = "Polymarket Chainlink 60-second TWAP current price";
  if (validOpen && validCurrent) {
    const exactMargin = Number(reference.margin);
    const delta = Number.isFinite(exactMargin) ? exactMargin : current - opening;
    $("priceMargin").textContent = `${delta >= 0 ? "+" : "−"}${fmt.usd(Math.abs(delta))}`;
    $("priceMargin").className = delta >= 0 ? "positive" : "negative";
  } else {
    $("priceMargin").textContent = "—";
    $("priceMargin").className = "";
  }
}

function renderMarketQuotes() {
  const latest = state.chart.market?.at(-1);
  const streamed = state.liveQuotes?.quotes;
  const prediction = state.live?.prediction || {};
  const up = streamed?.Up?.ask ?? streamed?.Up?.bid ?? latest?.up?.ask ?? latest?.up?.bid ?? prediction.entry_asks?.Up;
  const down = streamed?.Down?.ask ?? streamed?.Down?.bid ?? latest?.down?.ask ?? latest?.down?.bid ?? prediction.entry_asks?.Down;
  $("upAsk").textContent = fmt.cents(up);
  $("downAsk").textContent = fmt.cents(down);
  renderCalculator();
}

function liveOutcomePrice(side) {
  const latest = state.chart.market?.at(-1);
  const streamed = state.liveQuotes?.quotes?.[side];
  const prediction = state.live?.prediction || {};
  const quote = side === "Up" ? latest?.up : latest?.down;
  return Number(streamed?.ask ?? streamed?.bid ?? quote?.ask ?? quote?.bid ?? prediction.entry_asks?.[side]);
}

function calculatorPrice() {
  return liveOutcomePrice(calculator.side);
}

function renderCalculator() {
  const price = calculatorPrice();
  const valid = Number.isFinite(price) && price > 0 && price < 1;
  const amount = Number(calculator.amount);
  $("selectedSideLabel").textContent = calculator.side.toUpperCase();
  $("selectedSideLabel").className = sideClass(calculator.side);
  document.querySelectorAll(".outcome-button").forEach(button => button.classList.toggle("active", button.dataset.side === calculator.side));
  $("priceSource").textContent = valid ? `Live best ask · ${fmt.cents(price)}` : "Waiting for live price";
  document.querySelectorAll(".quick-amounts button").forEach((button, index) => {
    const preset = calculator.presets[index];
    button.dataset.amount = preset;
    button.querySelector("b").textContent = fmt.usd(preset, preset % 1 ? 2 : 0);
    button.classList.toggle("active", Math.abs(amount - preset) < .001);
    button.querySelector("em").textContent = valid ? fmt.usd(preset / price) : "—";
  });
  if (!valid || !Number.isFinite(amount) || amount <= 0) {
    $("estimatedShares").textContent = $("potentialPayout").textContent = $("potentialProfit").textContent = "—";
    updateManualBuyState();
    return;
  }
  const shares = amount / price;
  $("estimatedShares").textContent = fmt.num(shares, 2);
  $("potentialPayout").textContent = fmt.usd(shares);
  $("potentialProfit").textContent = fmt.usd(shares - amount);
  updateManualBuyState();
}

function shortAddress(address) {
  return address ? `${address.slice(0, 7)}…${address.slice(-5)}` : "—";
}

function updateManualBuyState() {
  const account = state.account || {};
  const market = account.current_market || {};
  const amount = Number(calculator.amount);
  const price = calculatorPrice();
  const meetsMinimum = amount + 1e-9 >= 1;
  const ready = account.connected && market.tradable && Number.isFinite(price) && price > 0 && Number.isFinite(amount) && amount > 0 && amount <= Number(account.balance_usdc || 0) && meetsMinimum;
  $("manualBuyButton").disabled = !ready;
  $("manualBuyButton").textContent = ready ? `Review ${calculator.side} buy · ${fmt.usd(amount)}` : "Review real buy";
  if (!account.configured) $("realTradeNote").textContent = "Add the real-account fields to .env, then restart the API.";
  else if (account.error) $("realTradeNote").textContent = account.error;
  else if (!account.connected) $("realTradeNote").textContent = "Connecting to Polymarket…";
  else if (!market.tradable) $("realTradeNote").textContent = market.reason || "Trading is unavailable for this round.";
  else if (amount > Number(account.balance_usdc || 0)) $("realTradeNote").textContent = `Available balance is ${fmt.usd(account.balance_usdc)}.`;
  else if (!meetsMinimum) $("realTradeNote").textContent = "Polymarket market buys require at least $1.";
  else $("realTradeNote").textContent = "Minimum market buy $1 · protected by the preview price limit.";
}

function renderAccount() {
  const account = state.account || {};
  const balance = account.balance_usdc == null ? null : Number(account.balance_usdc);
  $("realBalance").textContent = account.connected ? fmt.usd(account.account_value_usdc) : account.configured ? "Unavailable" : "Not configured";
  $("realBalance").className = account.connected ? "positive" : "negative";
  $("positionsBalance").textContent = account.connected ? fmt.usd(balance) : "—";
  $("accountAddress").textContent = account.wallet ? `${shortAddress(account.wallet)} · ${account.wallet_type || "wallet"}` : (account.error || "Account not configured");
  $("accountAddress").title = account.wallet || "";
  const positions = account.positions || [];
  renderActivePosition(positions);
  if (!positions.length) {
    $("realPositionList").innerHTML = `<div class="empty-state">${account.connected ? "No open positions on this wallet." : escapeHtml(account.error || "Connect an account to see positions.")}</div>`;
  } else {
    $("realPositionList").innerHTML = positions.map(position => {
      const pnl = Number(position.cash_pnl || 0) + Number(position.realized_pnl || 0);
      const canSell = position.tradable && Number(position.size) > 0;
      return `<div class="real-position">
        <div class="position-name"><span>${escapeHtml(position.outcome || "Position")}</span><strong>${escapeHtml(position.title || position.slug || "Polymarket")}</strong><small>${fmt.num(position.size, 2)} shares · avg ${fmt.cents(position.avg_price)} · mark ${fmt.cents(position.current_price)}</small></div>
        <div class="position-value"><span>Current value</span><b>${fmt.usd(position.current_value)}</b><em class="${pnl >= 0 ? "positive" : "negative"}">${pnl >= 0 ? "+" : ""}${fmt.usd(pnl)}</em></div>
        <div class="position-sell"><input aria-label="Shares to sell" type="number" min="0.000001" max="${escapeHtml(position.size)}" step="0.000001" value="${escapeHtml(position.size)}" ${canSell ? "" : "disabled"}><button class="sell-position" data-token="${escapeHtml(position.token_id)}" ${canSell ? "" : "disabled"}>${canSell ? "Review sell" : "Closed"}</button></div>
      </div>`;
    }).join("");
    document.querySelectorAll(".sell-position:not(:disabled)").forEach(button => button.addEventListener("click", () => {
      const shares = Number(button.parentElement.querySelector("input").value);
      if (!Number.isFinite(shares) || shares <= 0) return toast("Enter the shares you want to sell");
      previewSell(button.dataset.token, shares);
    }));
  }
  updateManualBuyState();
}

function renderActivePosition(positions) {
  const slug = state.account?.current_market?.slug;
  const position = positions.find(row => row.slug === slug && Number(row.size) > 0)
    || positions.find(row => Number(row.size) > 0);
  const panel = $("activePosition");
  panel.classList.toggle("hidden", !position);
  if (!position) {
    panel.dataset.token = "";
    return;
  }
  const pnl = Number(position.cash_pnl || 0) + Number(position.realized_pnl || 0);
  const mark = Number(position.current_price);
  const settled = position.slug !== slug || !position.tradable;
  const canSell = !settled && Number.isFinite(mark) && mark > 0;
  const status = position.redeemable ? " · WON" : settled ? " · SETTLED" : "";
  $("activePositionSide").textContent = `${String(position.outcome || "Position").toUpperCase()}${status}`;
  $("activePositionSide").className = sideClass(position.outcome);
  $("activePositionShares").textContent = fmt.num(position.size, 4);
  $("activePositionAverage").textContent = fmt.cents(position.avg_price);
  $("activePositionMark").textContent = fmt.cents(position.current_price);
  $("activePositionValue").textContent = fmt.usd(position.current_value);
  $("activePositionPnl").textContent = `${pnl >= 0 ? "+" : ""}${fmt.usd(pnl)}`;
  $("activePositionPnl").className = pnl >= 0 ? "positive" : "negative";
  const input = $("activePositionSellShares");
  if (document.activeElement !== input || input.dataset.token !== String(position.token_id)) {
    input.value = position.size;
  }
  input.max = position.size;
  input.dataset.token = String(position.token_id);
  input.disabled = !canSell;
  const button = $("activePositionSellButton");
  button.disabled = !canSell;
  button.textContent = canSell ? "Review sell" : position.redeemable ? "Redeemable" : "Selling closed";
  $("activePositionNote").textContent = position.redeemable
    ? "This round settled in your favor. Selling is closed and the winnings are ready to redeem."
    : canSell
    ? "Profit and loss uses the live executable bid and updates automatically."
    : "No executable bid is currently available, or this market has closed.";
}

function renderOrderPreview(preview) {
  const buy = preview.action === "BUY";
  $("orderDialogTitle").textContent = `${buy ? "Buy" : "Sell"} ${preview.outcome}`;
  $("orderReview").innerHTML = `
    <div><span>Market</span><b>${escapeHtml(preview.slug.replace("btc-updown-5m-", "BTC 5m · "))}</b></div>
    <div><span>${buy ? "Order amount" : "Shares to sell"}</span><b>${buy ? fmt.usd(preview.requested) : fmt.num(preview.requested, 4)}</b></div>
    <div><span>Estimated price</span><b>${fmt.cents(preview.estimated_price)}</b></div>
    <div><span>${buy ? "Maximum price" : "Minimum price"}</span><b>${fmt.cents(preview.price_bound)}</b></div>
    <div><span>Estimated shares</span><b>${fmt.num(preview.estimated_shares, 4)}</b></div>`;
  $("orderMessage").textContent = buy
    ? "This preview expires in 12 seconds. Polymarket fees may be charged separately from the order amount."
    : "This preview expires in 12 seconds.";
  $("confirmOrderButton").disabled = false;
  $("confirmOrderButton").textContent = `Confirm real ${buy ? "buy" : "sell"}`;
  $("orderDialog").showModal();
}

async function createOrderPreview(payload) {
  $("manualBuyButton").disabled = true;
  try {
    const preview = await request("/api/v1/account/orders/preview", { method: "POST", body: JSON.stringify(payload) });
    state.orderPreview = preview;
    renderOrderPreview(preview);
  } catch (error) { toast(error.message); }
  finally { updateManualBuyState(); }
}

function previewBuy() {
  const slug = state.account?.current_market?.slug;
  if (!slug) return toast("No current market is available");
  createOrderPreview({ action: "BUY", slug, outcome: calculator.side, amount_usd: Number(calculator.amount), slippage_bps: 100 });
}

function previewSell(tokenId, shares) {
  const slug = state.account?.current_market?.slug;
  if (!slug) return toast("This position is no longer tradable");
  createOrderPreview({ action: "SELL", slug, token_id: tokenId, shares, slippage_bps: 100 });
}

async function confirmRealOrder() {
  const preview = state.orderPreview;
  if (!preview) return;
  const button = $("confirmOrderButton");
  button.disabled = true;
  button.textContent = "Submitting…";
  $("orderMessage").textContent = "Waiting for Polymarket…";
  try {
    const result = await request("/api/v1/account/orders", { method: "POST", body: JSON.stringify({ preview_id: preview.preview_id }) });
    $("orderDialog").close();
    state.orderPreview = null;
    toast(`Order ${result.status || "accepted"} · syncing position`);
    refreshAccountAfterOrder(preview).catch(() => {});
  } catch (error) {
    $("orderMessage").textContent = error.message;
    button.textContent = "Order not sent · close and retry";
  }
}

async function refreshAccountAfterOrder(preview) {
  const before = Number((state.account?.positions || []).find(
    row => row.slug === preview.slug && row.outcome === preview.outcome)?.size || 0);
  for (let attempt = 0; attempt < 15; attempt += 1) {
    if (attempt) await new Promise(resolve => setTimeout(resolve, 800));
    state.account = await request("/api/v1/account");
    renderAccount();
    const current = Number((state.account.positions || []).find(
      row => row.slug === preview.slug && row.outcome === preview.outcome)?.size || 0);
    if ((preview.action === "BUY" && current > before) || (preview.action === "SELL" && current < before)) {
      toast(preview.action === "BUY" ? "Position is live and P&L is streaming" : "Sale reflected in your position");
      return;
    }
  }
}

function selectOutcome(side) {
  calculator.side = side;
  renderCalculator();
}

function renderStatistics() {
  const s = state.dashboard?.statistics || {};
  const values = [["Evaluated rounds", s.rounds ?? 0], ["Directional signals", s.signals ?? 0], ["Resolved signal accuracy", fmt.percent(s.signal_accuracy)], ["Automated real trades", s.trades ?? 0], ["Realized real P&L", fmt.usd(s.realized_profit)]];
  $("metrics").innerHTML = values.map(([label,value]) => `<div class="metric"><span>${label}</span><b>${value}</b></div>`).join("");
}

async function loadRounds() {
  const query = new URLSearchParams();
  const page = state.pages.history;
  query.set("limit", page.limit);
  query.set("offset", page.offset);
  if ($("resultFilter").value) query.set("result", $("resultFilter").value);
  if ($("predictionFilter").value) query.set("prediction", $("predictionFilter").value);
  if ($("tradedFilter").value) query.set("traded", $("tradedFilter").value);
  const result = await request(`/api/v1/rounds?${query}`);
  state.rounds = result.items;
  Object.assign(page, { total: result.total, offset: result.offset, limit: result.limit });
  $("historyBody").innerHTML = state.rounds.map(row => {
    const confidence = row.prediction === "Up" ? row.p_up : row.prediction === "Down" ? row.p_down : Math.max(Number(row.p_up || 0), Number(row.p_down || 0));
    const market = `Up ${fmt.cents(row.market_up_ask)} · Down ${fmt.cents(row.market_down_ask)}`;
    const tradeLabel = row.traded ? "Real" : "No";
    return `<tr data-slug="${row.slug}"><td><b>${fmt.date(row.start)}</b><br><small class="mono">${row.slug.replace("btc-updown-5m-", "#")}</small></td><td class="${sideClass(row.prediction)}"><b>${row.prediction}</b></td><td class="mono">${fmt.percent(confidence)}</td><td class="mono">${market}</td><td>${row.actual || "—"}</td><td>${badge(row.result)}</td><td class="mono">${fmt.percent(row.estimated_edge)}</td><td>${tradeLabel}</td></tr>`;
  }).join("");
  $("historyEmpty").classList.toggle("hidden", state.rounds.length > 0);
  renderPagination("history");
  prepareMobileTables();
  document.querySelectorAll("#historyBody tr").forEach(row => row.addEventListener("click", () => openRound(row.dataset.slug)));
}

async function openRound(slug) {
  try {
    const detail = await request(`/api/v1/rounds/${slug}`);
    const decision = detail.decision || {};
    $("roundDetail").innerHTML = `<p class="eyebrow">ROUND INSPECTION</p><h2>${slug}</h2><div class="signal-stats"><div><span>Decision</span><b class="${sideClass(decision.direction)}">${decision.direction || "—"}</b></div><div><span>UP probability</span><b>${fmt.percent(decision.p_up)}</b></div><div><span>DOWN probability</span><b>${fmt.percent(decision.p_down)}</b></div><div><span>Result</span><b>${detail.resolution?.actual_result || "Pending"}</b></div></div><div class="detail-section"><h3>DECISION REASON</h3><p>${humanReason(decision.reason)}</p></div><div class="detail-section"><h3>AGREEMENT</h3><pre class="detail-json"></pre></div><div class="detail-section"><h3>COMPONENTS</h3><pre class="detail-json"></pre></div>`;
    const blocks = $("roundDetail").querySelectorAll("pre");
    blocks[0].textContent = JSON.stringify(decision.agreement || {}, null, 2);
    blocks[1].textContent = JSON.stringify(decision.components || {}, null, 2);
    $("roundDialog").showModal();
  } catch (error) { toast(error.message); }
}

function isTerminalSession(session) {
  return ["STOPPED", "COMPLETED", "GAME_OVER"].includes(session?.status);
}

function riskRuleText() {
  return {
    title: "Fixed high-confidence protection",
    copy: "The bot requires 98% confidence, strength above 2.5, market agreement, no thin gap, no movement disagreement, and a fresh executable price above 80¢ and at most 97¢.",
    budget: "80% of trading capital · profits protected",
    session: "strength >2.5; thin gaps and movement disagreement skipped; 80–97¢ entry",
  };
}

function activeSession() {
  return state.sessions.find(session => !isTerminalSession(session))
    || state.activeSessionSummary || null;
}

function sessionAvailable(session) {
  return session?.status_detail?.trading_capital
    ?? session?.status_detail?.available_bankroll;
}

function liveTradeState(trade, session) {
  if (trade.status !== "OPEN") return trade;
  const position = (state.account?.positions || []).find(row =>
    row.slug === trade.slug && row.outcome === trade.side);
  const start = Number(String(trade.slug || "").split("-").at(-1));
  const price = Number(position?.current_price);
  if (!position || !Number.isFinite(start) || Date.now() / 1000 < start + 300
      || !Number.isFinite(price)) return trade;
  let won = null;
  if (position.redeemable === true || price >= .999) won = true;
  else if (price <= .001) won = false;
  if (won == null) return trade;
  return { ...trade, status: won ? "WIN" : "LOSS",
    profit: won ? Number(trade.shares || 0) - Number(trade.cost || 0)
      : -Number(trade.cost || 0), liveSettlement: true };
}

function renderTradingControl() {
  const runtime = Boolean(state.dashboard?.capabilities?.automatic_real_trading);
  const active = activeSession();
  const stopping = active?.status === "STOPPING";
  const accepting = Boolean(active?.status_detail?.accepting_entries) && !stopping;
  const button = $("autoTradingButton");
  button.textContent = active ? (stopping ? "Stopping…" : "Stop bot") : "Start bot";
  button.disabled = !runtime || stopping;
  button.classList.toggle("danger-action", Boolean(active && !stopping));
  $("autoTradingStatus").textContent = !runtime ? "UNAVAILABLE" : active ? active.status.replaceAll("_", " ") : "READY";
  $("autoTradingStatus").className = `pill ${accepting ? "signal" : active ? "warning" : "muted"}`;
  $("autoTradingCopy").textContent = !runtime
    ? "Trading sessions require the collector runtime."
    : active
      ? `REAL · ${fmt.usd(sessionAvailable(active))} trading capital · ${fmt.usd(active.status_detail?.protected_profit || 0)} protected profit.`
      : "One click starts the fixed strategy. It uses 80% of remaining capital and never reuses session profit.";
}

function renderSessionList() {
  state.sessions.sort((a, b) => Number(b.created_ms || 0) - Number(a.created_ms || 0));
  $("sessionsEmpty").classList.toggle("hidden", state.sessions.length > 0);
  $("sessionList").innerHTML = state.sessions.map(session => `<button class="session-item ${session.session_id === state.selectedSessionId ? "active" : ""}" data-session-id="${session.session_id}"><div class="session-item-head"><strong>${escapeHtml(session.name)}</strong>${badge(session.status)}</div><small>${new Date(session.created_ms).toLocaleString()}</small><div class="session-item-stats"><span>CAPITAL<b>${fmt.usd(sessionAvailable(session))}</b></span><span>SAFE<b>${fmt.usd(session.status_detail?.protected_profit || 0)}</b></span><span>TRADES<b>${session.trade_count}</b></span></div></button>`).join("");
  document.querySelectorAll(".session-item").forEach(button => button.addEventListener("click", () => selectSession(button.dataset.sessionId)));
  renderTradingControl();
}

function renderSessionDetail() {
  const session = state.sessionDetail;
  const trades = (session?.trades || []).map(row => liveTradeState(row, session));
  state.trades = trades;
  if (!session) {
    $("sessionLedgerTitle").textContent = "Select a session";
    $("sessionRules").textContent = "";
    $("tradeMetrics").innerHTML = "";
  } else {
    const config = session.configuration;
    $("sessionLedgerTitle").textContent = session.name;
    $("sessionRules").textContent = `REAL · 98% confidence · strength >2.5 · 80% per entry · ${riskRuleText().session} · fresh validation until 30s · manual stop`;
    const values = [["Starting", fmt.usd(config.starting_bankroll)], ["Trading capital", fmt.usd(sessionAvailable(session))], ["Protected profit", fmt.usd(session.status_detail?.protected_profit || 0)], ["Account cash", fmt.usd(session.status_detail?.account_balance)], ["Trades", session.trade_count], ["Wins / losses", `${session.wins} / ${session.losses}`], ["Realized P&L", fmt.usd(session.realized_profit)]];
    $("tradeMetrics").innerHTML = values.map(([label,value]) => `<div class="metric"><span>${label}</span><b>${value}</b></div>`).join("");
  }
  $("tradesBody").innerHTML = trades.map(row => `<tr><td>${row.slug.replace("btc-updown-5m-", "#")}</td><td class="${sideClass(row.side)}"><b>${row.side}</b></td><td>${fmt.cents(row.entry_price)}</td><td class="mono">${fmt.num(row.shares, 3)}</td><td>${fmt.usd(row.cost)}</td><td>${badge(row.status)}</td><td class="${row.profit >= 0 ? "positive" : "negative"}">${fmt.usd(row.profit)}</td></tr>`).join("");
  $("tradesEmpty").classList.toggle("hidden", trades.length > 0);
  const tradePage = session?.trades_page || { total: trades.length, offset: 0, limit: state.pages.trades.limit };
  const eventPage = session?.events_page || { total: session?.events?.length || 0, offset: 0, limit: state.pages.events.limit };
  Object.assign(state.pages.trades, tradePage);
  Object.assign(state.pages.events, eventPage);
  renderPagination("trades");
  renderPagination("events");
  const events = session?.events || [];
  $("sessionEventCount").textContent = `${eventPage.total} EVENT${eventPage.total === 1 ? "" : "S"}`;
  $("sessionEvents").innerHTML = events.length ? events.map(event => `<div class="session-event"><i class="${event.event || "event"}"></i><div><strong>${escapeHtml(String(event.event || "event").replaceAll("_", " ").toUpperCase())}</strong><p>${escapeHtml(sessionEventDescription(event))}</p></div><time>${event.recorded_ms ? new Date(event.recorded_ms).toLocaleTimeString() : "—"}</time></div>`).join("") : `<div class="empty-state">${session ? "No activity recorded yet." : "Select a session to inspect its activity."}</div>`;
  prepareMobileTables();
}

function sessionEventDescription(event) {
  const round = event.slug ? event.slug.replace("btc-updown-5m-", "#") : null;
  if (event.event === "signal_seen") return `${round} · ${event.side} ${fmt.percent(event.chance)} · maximum ${fmt.cents(event.maximum_value_price)}`;
  if (event.event === "trade_filled") return `${round} · ${event.side} ${fmt.num(event.shares, 2)} shares at ${fmt.cents(event.price)} · cost ${fmt.usd(event.trade_cost)}`;
  if (event.event === "trade_resolved") return `${round} · ${event.won ? "won" : "lost"} · profit ${fmt.usd(event.profit)}`;
  if (event.event === "entry_expired") return `${round} · no acceptable fill before the entry cutoff`;
  if (event.event === "entry_cancelled") return `${round} · ${String(event.reason || "cancelled").replaceAll("_", " ")}`;
  if (event.event === "session_stopped" || event.event === "game_over") return String(event.reason || "session ended").replaceAll("_", " ");
  if (event.event === "run_started") return `Started with ${fmt.usd(event.starting_bankroll)} available cash`;
  if (event.event === "order_preflight_rejected") return `${round} · exchange preflight: ${event.message || "rejected"}`;
  if (event.event === "order_submission_uncertain") return `${round} · submission result uncertain; retry suppressed to prevent a duplicate order`;
  if (event.event === "order_rejected") return `${round} · Polymarket rejected the FOK order`;
  if (event.event === "configuration_updated") return `Runtime rules updated · next budget ${fmt.usd(event.planned_allocation)}`;
  if (event.event === "signal_rejected") return `${round || "Signal"} · ${String(event.reason || "risk rule").replaceAll("_", " ")}`;
  if (event.event === "recovered_signal_watching") return `${round} · initially blocked by ${String(event.initial_rejection_reason || "a rule").replaceAll("_", " ")} · shadow monitoring started`;
  if (event.event === "recovered_signal_passed") return `${round} · ${event.side} passed ≥90% probability, >2.5 strength and ≥5 bps gap after ${event.confirmation_snapshots || 0} confirmed snapshots`;
  if (event.event === "recovered_opportunity") return `${round} · fillable ${event.side} at ${fmt.cents(event.price)} · ${event.execution_reason === "recovered_execution_disabled" ? "logged without trading" : String(event.execution_reason || "opportunity logged").replaceAll("_", " ")}`;
  if (event.event === "recovered_opportunity_resolved") return `${round} · hypothetical ${event.won ? "WIN" : "LOSS"} · ${fmt.usd(event.hypothetical_profit)}`;
  if (event.event === "recovered_execution_updated") return `Recovered-signal execution ${event.enabled ? "enabled" : "disabled"}`;
  if (event.event === "fund_transfer") return `${fmt.usd(event.amount)} moved from ${String(event.source).replaceAll("_", " ")} to ${String(event.destination).replaceAll("_", " ")}`;
  return round || String(event.reason || "Session state updated").replaceAll("_", " ");
}

function renderRecoveredSignals() {
  const funds = state.funds || {};
  const active = Boolean(funds.active && funds.run_id);
  const enabled = Boolean(funds.recovered_signals_enabled);
  $("safeReserveBalance").textContent = fmt.usd(funds.safe_reserve);
  $("coreTradingBalance").textContent = fmt.usd(funds.core_trading);
  $("profitTradingBalance").textContent = fmt.usd(funds.profit_trading);
  $("profitLiquidBalance").textContent = fmt.usd(funds.account_balance);
  $("recoveredExecutionStatus").textContent = !active ? "BOT OFF" : enabled ? "ACTIVE" : "ANALYSIS ONLY";
  $("recoveredExecutionStatus").className = `pill ${enabled && active ? "signal" : active ? "warning" : "muted"}`;
  $("recoveredExecutionButton").textContent = enabled ? "Disable trading" : "Enable trading";
  $("recoveredExecutionButton").disabled = !active;
  $("recoveredExecutionButton").classList.toggle("danger-action", enabled);
  $("recoveredExecutionCopy").textContent = enabled
    ? "Execution is active. The fresh side must reach ≥90% probability, >2.5 strength and ≥5 bps gap; each entry uses 50% of the profit wallet."
    : "Analysis stays active using ≥90% probability, >2.5 strength and ≥5 bps gap without spending money.";

  const stats = funds.statistics || {};
  const values = [
    ["Watched", stats.watched || 0],
    ["Fillable recoveries", stats.opportunities || 0],
    ["Hypothetical W / L", `${stats.hypothetical_wins || 0} / ${stats.hypothetical_losses || 0}`],
    ["Hypothetical P&L", fmt.usd(stats.hypothetical_profit || 0)],
    ["Real recovered trades", stats.real_trades || 0],
    ["Real recovered P&L", fmt.usd(stats.realized_profit || 0)],
  ];
  $("recoveredMetrics").innerHTML = values.map(([label, value]) => `<div class="metric"><span>${label}</span><b>${value}</b></div>`).join("");

  const candidates = funds.recovered_candidates || [];
  $("recoveredCandidateCount").textContent = `${candidates.length} ACTIVE`;
  $("recoveredCandidates").innerHTML = candidates.length ? candidates.map(candidate => `<div class="recovered-candidate"><div><strong>${escapeHtml(candidate.slug.replace("btc-updown-5m-", "#"))} · <span class="${sideClass(candidate.side)}">${escapeHtml(candidate.side)}</span></strong><p>Initial block: ${escapeHtml(String(candidate.initial_rejection_reason || "unknown").replaceAll("_", " "))}</p></div><div><b>${fmt.percent(candidate.chance)}</b><small>strength ${fmt.num(candidate.strength, 2)}</small></div><em>${escapeHtml(String(candidate.last_validation_reason || "collecting fresh checks").replaceAll("_", " "))}</em></div>`).join("") : `<div class="empty-state">No rejected signal is currently being monitored.</div>`;

  const activity = funds.activity || [];
  const activityPage = funds.activity_page || {
    total: activity.length, offset: 0, limit: state.pages.recovered.limit,
  };
  Object.assign(state.pages.recovered, activityPage);
  renderPagination("recovered");
  $("recoveredActivity").innerHTML = activity.length ? activity.map(event => `<div class="session-event"><i class="${event.event || "event"}"></i><div><strong>${escapeHtml(String(event.event || "event").replaceAll("_", " ").toUpperCase())}</strong><p>${escapeHtml(sessionEventDescription(event))}</p></div><time>${event.recorded_ms ? new Date(event.recorded_ms).toLocaleTimeString() : "—"}</time></div>`).join("") : `<div class="empty-state">No recovered-signal activity yet.</div>`;
}

async function loadRecoveredSignals() {
  const page = state.pages.recovered;
  const query = new URLSearchParams({
    activity_limit: page.limit,
    activity_offset: page.offset,
  });
  state.funds = await request(`/api/v1/trading/funds?${query}`);
  const returnedPage = state.funds.activity_page;
  if (returnedPage && page.offset >= returnedPage.total && returnedPage.total) {
    page.offset = Math.floor((returnedPage.total - 1) / page.limit) * page.limit;
    return loadRecoveredSignals();
  }
  renderRecoveredSignals();
}

async function toggleRecoveredExecution() {
  const funds = state.funds || {};
  if (!funds.run_id || !funds.active) return toast("Start the main trading bot first");
  const button = $("recoveredExecutionButton");
  button.disabled = true;
  try {
    state.funds = await request(`/api/v1/trading/sessions/${encodeURIComponent(funds.run_id)}/recovered-signals`, {
      method: "PATCH", body: JSON.stringify({ enabled: !funds.recovered_signals_enabled }),
    });
    toast(`Recovered-signal trading ${state.funds.recovered_signals_enabled ? "enabled" : "disabled"}`);
  } catch (error) { toast(error.message); }
  finally { renderRecoveredSignals(); }
}

async function transferFunds() {
  const funds = state.funds || {};
  const source = $("fundTransferSource").value;
  const destination = $("fundTransferDestination").value;
  const amount = Number($("fundTransferAmount").value);
  if (!funds.run_id || !funds.active) return toast("Start the main trading bot first");
  if (source === destination) return toast("Choose two different wallets");
  if (!Number.isFinite(amount) || amount <= 0) return toast("Enter a valid amount");
  const button = $("fundTransferButton");
  button.disabled = true;
  try {
    state.funds = await request(`/api/v1/trading/sessions/${encodeURIComponent(funds.run_id)}/fund-transfers`, {
      method: "POST", body: JSON.stringify({ source, destination, amount }),
    });
    $("fundTransferAmount").value = "";
    toast("Funds moved successfully");
    await loadRecoveredSignals();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; renderRecoveredSignals(); }
}

async function selectSession(sessionId) {
  state.selectedSessionId = sessionId;
  state.pages.trades.offset = 0;
  state.pages.events.offset = 0;
  state.sessionDetail = await loadSelectedSession();
  renderSessionList();
  renderSessionDetail();
}

async function loadSelectedSession() {
  if (!state.selectedSessionId) return null;
  const query = new URLSearchParams({
    trade_limit: state.pages.trades.limit,
    trade_offset: state.pages.trades.offset,
    event_limit: state.pages.events.limit,
    event_offset: state.pages.events.offset,
  });
  if ($("tradeStatusFilter").value) query.set("trade_status", $("tradeStatusFilter").value);
  const session = await request(`/api/v1/trading/sessions/${encodeURIComponent(state.selectedSessionId)}?${query}`);
  // Older running API processes ignore the paging query until their next restart.
  // Slice here as a temporary compatibility path; the new API returns page metadata.
  if (!session.trades_page) {
    let trades = session.trades || [];
    const status = $("tradeStatusFilter").value;
    if (status) trades = trades.filter(trade => trade.status === status);
    session.trades_page = { total: trades.length, offset: state.pages.trades.offset, limit: state.pages.trades.limit };
    session.trades = trades.slice(state.pages.trades.offset, state.pages.trades.offset + state.pages.trades.limit);
  }
  if (!session.events_page) {
    const events = (session.events || []).slice().reverse();
    session.events_page = { total: events.length, offset: state.pages.events.offset, limit: state.pages.events.limit };
    session.events = events.slice(state.pages.events.offset, state.pages.events.offset + state.pages.events.limit);
  }
  return session;
}

async function reloadSelectedSession() {
  state.sessionDetail = await loadSelectedSession();
  renderSessionDetail();
}

async function loadSessions() {
  const page = state.pages.sessions;
  const result = await request(`/api/v1/trading/sessions?limit=${page.limit}&offset=${page.offset}`);
  const serverPaged = Number.isFinite(Number(result.total));
  const allItems = result.items || [];
  state.sessions = serverPaged ? allItems : allItems.slice(page.offset, page.offset + page.limit);
  state.activeSessionSummary = result.active_session
    || allItems.find(session => !isTerminalSession(session)) || null;
  Object.assign(page, { total: serverPaged ? Number(result.total) : allItems.length,
    offset: result.offset ?? page.offset, limit: result.limit ?? page.limit });
  if (page.offset >= page.total && page.total) {
    page.offset = Math.floor((page.total - 1) / page.limit) * page.limit;
    return loadSessions();
  }
  if (!state.selectedSessionId || !state.sessions.some(session => session.session_id === state.selectedSessionId)) {
    state.selectedSessionId = state.sessions[0]?.session_id || null;
  }
  renderSessionList();
  renderPagination("sessions");
  if (state.selectedSessionId && $("tradesView").classList.contains("active")) {
    state.sessionDetail = await loadSelectedSession();
  } else if (!state.selectedSessionId) {
    state.sessionDetail = null;
  }
  renderSessionDetail();
  if (state.live) renderLive();
}

async function toggleAutomaticTrading() {
  const button = $("autoTradingButton");
  const active = activeSession();
  button.disabled = true;
  try {
    if (active) {
      await request(`/api/v1/trading/sessions/${encodeURIComponent(active.session_id)}/stop`, { method: "POST" });
      active.status = "STOPPING";
      active.status_detail.accepting_entries = false;
      toast("Real-money bot is stopping");
    } else {
      const cash = Number(state.account?.balance_usdc);
      if (!state.account?.connected || !Number.isFinite(cash) || cash < 1.25) {
        throw new Error(state.account?.error || "At least $1.25 available USDC is required for an 80% order");
      }
      const session = await request("/api/v1/trading/sessions", {
        method: "POST", body: JSON.stringify({}),
      });
      state.selectedSessionId = session.session_id;
      toast("Real-money bot started");
    }
    await loadSessions();
  } catch (error) {
    toast(error.message);
    await loadSessions().catch(() => {});
  } finally {
    renderTradingControl();
  }
}

async function refresh() {
  try {
    const [dashboard, chart, health, account] = await Promise.all([request("/api/v1/dashboard"), request("/api/v1/live/chart"), request("/health"), request("/api/v1/account")]);
    state.dashboard = dashboard; state.live = dashboard.live; state.chart = chart; state.health = health; state.account = account;
    $("collectorStatus").textContent = health.collector.fresh ? "Live" : health.collector.available ? "Stale" : "Offline";
    $("collectorStatus").className = health.collector.fresh ? "positive" : "negative";
    renderLive(); renderCharts(); renderStatistics(); renderAccount();
    const followups = [loadSessions(), loadRecoveredSignals()];
    if ($("historyView").classList.contains("active")) followups.push(loadRounds());
    await Promise.all(followups);
  } catch (error) {
    setConnection("offline", "API unavailable");
    toast(`API unavailable: ${error.message}`);
  }
}

function connectSockets() {
  clearTimeout(reconnectTimer);
  for (const socket of [liveSocket, chartSocket, quoteSocket, referenceSocket, accountSocket]) {
    if (socket) { socket.onclose = null; socket.close(); }
  }
  liveSocket = new WebSocket(`${WS}/api/v1/ws/live`);
  chartSocket = new WebSocket(`${WS}/api/v1/ws/chart`);
  quoteSocket = new WebSocket(`${WS}/api/v1/ws/quotes`);
  referenceSocket = new WebSocket(`${WS}/api/v1/ws/reference`);
  accountSocket = new WebSocket(`${WS}/api/v1/ws/account`);
  liveSocket.onopen = () => setConnection("online", "Live stream connected");
  liveSocket.onmessage = event => {
    const previousSlug = state.live?.prediction?.slug;
    state.live = JSON.parse(event.data);
    renderLive();
    if (previousSlug && state.live?.prediction?.slug !== previousSlug) {
      Promise.all([loadRounds(), loadSessions(), loadRecoveredSignals(), request("/api/v1/statistics")]).then(([, , , statistics]) => {
        state.dashboard.statistics = statistics;
        renderStatistics();
      }).catch(() => {});
    }
  };
  chartSocket.onmessage = event => { state.chart = JSON.parse(event.data); renderCharts(); };
  quoteSocket.onmessage = event => {
    const payload = JSON.parse(event.data);
    state.liveQuotes = payload;
    renderMarketQuotes();
  };
  referenceSocket.onmessage = event => {
    const payload = JSON.parse(event.data);
    if (state.liveReference?.slug !== payload.slug) state.realtimeBtc = [];
    state.liveReference = payload;
    const twapPoint = payload.prices?.chainlink_twap_60s;
    const twap = Number(payload.current_price ?? twapPoint?.value);
    const opening = Number(payload.opening_price);
    if (Number.isFinite(twap) && twap > 0) {
      state.realtimeBtc.push({
        timestamp_ms: twapPoint?.received_ms ?? Date.now(),
        price: twap,
        opening_price: Number.isFinite(opening) && opening > 0 ? opening : null,
      });
      if (state.realtimeBtc.length > 600) state.realtimeBtc.shift();
    }
    renderReferenceNumbers();
    renderCharts();
  };
  accountSocket.onmessage = event => {
    state.account = JSON.parse(event.data);
    renderAccount();
    renderSessionList();
    renderSessionDetail();
  };
  const reconnect = () => { setConnection("offline", "Reconnecting"); clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connectSockets, 2500); };
  liveSocket.onclose = reconnect; liveSocket.onerror = () => liveSocket.close();
  chartSocket.onclose = reconnect; chartSocket.onerror = () => chartSocket.close();
  quoteSocket.onclose = reconnect; quoteSocket.onerror = () => quoteSocket.close();
  referenceSocket.onclose = reconnect; referenceSocket.onerror = () => referenceSocket.close();
  accountSocket.onclose = reconnect; accountSocket.onerror = () => accountSocket.close();
}

function changeView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(v => v.classList.toggle("active", v.dataset.view === name));
  $(`${name}View`).classList.add("active");
  const titles = { dashboard: ["LIVE OPERATIONS", "Market overview"], history: ["PERFORMANCE", "Round history"], trades: ["BOT EXECUTION", "Trade ledger"], profit: ["PROTECTED PROFIT", "Recovered-signal trading"] };
  [$("viewEyebrow").textContent, $("viewTitle").textContent] = titles[name];
  history.replaceState(null, "", `#${name}`);
  if (name === "history") loadRounds().catch(error => toast(error.message));
  if (name === "trades") loadSessions().catch(error => toast(error.message));
  if (name === "profit") loadRecoveredSignals().catch(error => toast(error.message));
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => changeView(button.dataset.view)));
[$("resultFilter"), $("predictionFilter"), $("tradedFilter")].forEach(input => input.addEventListener("change", () => {
  state.pages.history.offset = 0;
  loadRounds().catch(e => toast(e.message));
}));
$("tradeStatusFilter").addEventListener("change", () => {
  state.pages.trades.offset = 0;
  reloadSelectedSession().catch(error => toast(error.message));
});
$("historyPrevious").addEventListener("click", () => movePage("history", -1, loadRounds));
$("historyNext").addEventListener("click", () => movePage("history", 1, loadRounds));
$("sessionsPrevious").addEventListener("click", () => movePage("sessions", -1, loadSessions));
$("sessionsNext").addEventListener("click", () => movePage("sessions", 1, loadSessions));
$("tradesPrevious").addEventListener("click", () => movePage("trades", -1, reloadSelectedSession));
$("tradesNext").addEventListener("click", () => movePage("trades", 1, reloadSelectedSession));
$("eventsPrevious").addEventListener("click", () => movePage("events", -1, reloadSelectedSession));
$("eventsNext").addEventListener("click", () => movePage("events", 1, reloadSelectedSession));
$("recoveredPrevious").addEventListener("click", () => movePage("recovered", -1, loadRecoveredSignals));
$("recoveredNext").addEventListener("click", () => movePage("recovered", 1, loadRecoveredSignals));
$("recoveredExecutionButton").addEventListener("click", toggleRecoveredExecution);
$("fundTransferButton").addEventListener("click", transferFunds);
$("autoTradingButton").addEventListener("click", toggleAutomaticTrading);
$("newSessionButton").addEventListener("click", toggleAutomaticTrading);
$("refreshButton").addEventListener("click", refresh);
$("dialogClose").addEventListener("click", () => $("roundDialog").close());
$("manualBuyButton").addEventListener("click", previewBuy);
$("activePositionSellButton").addEventListener("click", () => {
  const input = $("activePositionSellShares");
  const shares = Number(input.value);
  if (!Number.isFinite(shares) || shares <= 0) return toast("Enter the shares you want to sell");
  if (shares > Number(input.max)) return toast("Sell shares exceed your position");
  previewSell(input.dataset.token, shares);
});
$("orderDialogClose").addEventListener("click", () => $("orderDialog").close());
$("cancelOrderButton").addEventListener("click", () => $("orderDialog").close());
$("confirmOrderButton").addEventListener("click", confirmRealOrder);
$("orderDialog").addEventListener("close", () => { state.orderPreview = null; });
document.querySelectorAll(".outcome-button").forEach(button => button.addEventListener("click", () => selectOutcome(button.dataset.side)));
document.querySelectorAll(".quick-amounts button").forEach(button => button.addEventListener("click", () => {
  calculator.amount = Number(button.dataset.amount);
  $("cashAmount").value = calculator.amount;
  renderCalculator();
}));
$("cashAmount").addEventListener("input", event => {
  calculator.amount = Number(event.target.value);
  renderCalculator();
});
$("editAmountsButton").addEventListener("click", () => {
  const editor = $("amountEditor");
  editor.querySelectorAll("input").forEach((input, index) => input.value = calculator.presets[index]);
  editor.classList.toggle("hidden");
  if (!editor.classList.contains("hidden")) editor.querySelector("input").focus();
});
$("saveAmountsButton").addEventListener("click", () => {
  const values = [...$("amountEditor").querySelectorAll("input")].map(input => Number(input.value));
  if (values.some(value => !Number.isFinite(value) || value < 1)) {
    toast("Every quick amount must be at least $1");
    return;
  }
  calculator.presets = values;
  $("amountEditor").classList.add("hidden");
  renderCalculator();
  toast("Quick amounts updated");
});
$("resetAmountsButton").addEventListener("click", () => {
  calculator.presets = [1, 50, 100];
  calculator.amount = 50;
  $("cashAmount").value = 50;
  $("amountEditor").classList.add("hidden");
  renderCalculator();
});

setInterval(() => {
  $("clock").textContent = new Date().toLocaleTimeString("en-GB", { timeZone: "Africa/Lagos", hour12: false }) + " WAT";
  const element = $("countdown"), base = Number(element.dataset.remaining), updated = Number(element.dataset.updated);
  if (element.dataset.remaining !== "" && Number.isFinite(base) && Number.isFinite(updated)) {
    element.textContent = fmt.time(base - (Date.now() - updated) / 1000);
  }
}, 1000);

setInterval(async () => {
  if (state.dashboard?.capabilities?.automatic_real_trading) {
    if (!sessionPollInFlight) {
      sessionPollInFlight = true;
      try {
        await loadSessions();
        if ($("profitView").classList.contains("active")) await loadRecoveredSignals();
      } catch (_) {} finally { sessionPollInFlight = false; }
    }
  }
}, 5000);

const initialView = location.hash.slice(1);
if (["history", "trades", "profit"].includes(initialView)) changeView(initialView);
refresh();
connectSockets();
