const API = (window.BOT_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const WS = API.replace(/^http/, "ws");
const $ = (id) => document.getElementById(id);
const state = { dashboard: null, live: null, liveQuotes: null, liveReference: null, realtimeBtc: [], chart: { btc: [], market: [] }, health: null, rounds: [], trades: [], sessions: [], activeSessionSummary: null, selectedSessionId: null, sessionDetail: null, settings: null, account: null, orderPreview: null, models: [], defaultModel: "btc_twap_v2", modelRaces: [], selectedRaceId: null, raceDetail: null,
  pages: {
    history: { offset: 0, limit: 25, total: 0 },
    sessions: { offset: 0, limit: 8, total: 0 },
    trades: { offset: 0, limit: 25, total: 0 },
    events: { offset: 0, limit: 25, total: 0 },
    race: { offset: 0, limit: 25, total: 0 },
  },
};
const calculator = { side: "Up", amount: 50, presets: [1, 50, 100] };
let liveSocket, chartSocket, quoteSocket, referenceSocket, accountSocket, reconnectTimer;
let sessionPollInFlight = false, racePollInFlight = false;

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

function activeModelId() {
  return activeSession()?.configuration?.model_version || state.defaultModel;
}

function modelOutput(prediction, modelId) {
  const complete = prediction?.model_outputs?.[modelId];
  if (complete) return { ...complete, complete: true };
  const probability = prediction?.model_probabilities?.[modelId];
  if (probability) {
    const pUp = Number(probability.p_up);
    const side = probability.side || (pUp >= .5 ? "Up" : "Down");
    const chance = probability.chance == null ? Math.max(pUp, 1 - pUp) : Number(probability.chance);
    const isRecordedPrimary = prediction.model_version === modelId;
    return { ...probability, p_up: pUp, p_down: 1 - pUp, side, chance,
      direction: isRecordedPrimary ? prediction.direction : null,
      reason: isRecordedPrimary ? prediction.reason : "historical_probability_only",
      estimated_edge: isRecordedPrimary ? prediction.estimated_edge : null,
      agreement: isRecordedPrimary ? prediction.agreement : null,
      warnings: isRecordedPrimary ? prediction.warnings : [], complete: false };
  }
  if (prediction?.model_version === modelId && prediction.p_up != null) {
    const pUp = Number(prediction.p_up);
    return { ...prediction, side: pUp >= .5 ? "Up" : "Down",
      chance: Math.max(pUp, 1 - pUp), complete: false };
  }
  return null;
}

function modelDecision(output) {
  if (!output) return "WAITING";
  if (output.direction === "Up" || output.direction === "Down") return `TRADE ${output.direction.toUpperCase()}`;
  if (output.direction === "SKIP") return "SKIP";
  return output.complete ? "ANALYZING" : "PROBABILITY ONLY";
}

function polymarketComparisonMarkup(prediction) {
  const rawPUp = prediction?.components?.market?.p_up ?? prediction?.market_p_up;
  const rawUpAsk = prediction?.quotes?.Up?.ask ?? prediction?.market_up_ask;
  const rawDownAsk = prediction?.quotes?.Down?.ask ?? prediction?.market_down_ask;
  const pUp = rawPUp == null ? null : Number(rawPUp);
  const upAsk = rawUpAsk == null ? null : Number(rawUpAsk);
  const downAsk = rawDownAsk == null ? null : Number(rawDownAsk);
  const upPrice = Number.isFinite(upAsk) ? upAsk : Number.isFinite(pUp) ? pUp : null;
  const downPrice = Number.isFinite(downAsk) ? downAsk : Number.isFinite(pUp) ? 1 - pUp : null;
  const usedFallback = !Number.isFinite(upAsk) || !Number.isFinite(downAsk);
  const marketCents = value => {
    if (!Number.isFinite(value)) return "—";
    const cents = value * 100;
    return `${Number.isInteger(cents) ? cents.toFixed(0) : cents.toFixed(1)}¢`;
  };
  const side = Number.isFinite(pUp) ? (pUp >= .5 ? "Up" : "Down")
    : Number.isFinite(upPrice) && Number.isFinite(downPrice) ? (upPrice >= downPrice ? "Up" : "Down") : null;
  const favoredPrice = side === "Up" ? upPrice : side === "Down" ? downPrice : null;
  return `<div class="model-result market-reference ${sideClass(side)}">
    <div class="model-result-head"><strong>Polymarket</strong><span>MARKET</span></div>
    <div class="model-result-signal"><b>${side || "—"}</b><em>${marketCents(favoredPrice)}</em></div>
    <small>UP ${marketCents(upPrice)} · DOWN ${marketCents(downPrice)}${usedFallback ? " · MIDPOINT FALLBACK" : " · RECORDED ASKS"}</small>
  </div>`;
}

function modelComparisonMarkup(prediction, selectedId, compact = false) {
  const models = state.models.map(release => {
    const output = modelOutput(prediction, release.id);
    const side = output?.side;
    const chance = output?.chance;
    const decision = modelDecision(output);
    const selected = release.id === selectedId;
    return `<div class="model-result ${selected ? "selected" : ""} ${sideClass(side)}">
      <div class="model-result-head"><strong>${escapeHtml(release.name)}</strong>${selected ? `<span>ACTIVE</span>` : release.default ? `<span>DEFAULT</span>` : ""}</div>
      <div class="model-result-signal"><b>${side || "—"}</b><em>${fmt.percent(chance)}</em></div>
      <small class="${decision.startsWith("TRADE") ? "positive" : decision === "SKIP" ? "warning-text" : ""}">${decision}</small>
      ${compact ? "" : `<p>${escapeHtml(output ? humanReason(output.reason) : "Waiting for the decision snapshot.")}</p>`}
    </div>`;
  }).join("");
  return models + polymarketComparisonMarkup(prediction);
}

function modelAnalysisMarkup(prediction, selectedId, actual = null) {
  return state.models.map(release => {
    const output = modelOutput(prediction, release.id);
    const result = !actual || !output ? null
      : output.direction === "Up" || output.direction === "Down"
        ? (output.direction === actual ? "WIN" : "LOSS") : "SKIP";
    return `<div class="model-analysis-row ${release.id === selectedId ? "selected" : ""}">
      <div><strong>${escapeHtml(release.name)}</strong><small>${release.id === selectedId ? "Selected trading model" : release.default ? "Dashboard default" : "Comparison model"}</small></div>
      <div><span>Forecast</span><b class="${sideClass(output?.side)}">${output?.side || "—"} ${fmt.percent(output?.chance)}</b></div>
      <div><span>Decision</span><b>${modelDecision(output)}</b></div>
      <div><span>Edge</span><b>${fmt.percent(output?.estimated_edge)}</b></div>
      ${result ? `<div><span>Outcome</span><b>${badge(result)}</b></div>` : ""}
      <p>${escapeHtml(output ? humanReason(output.reason) : "Waiting for this model's analysis.")}</p>
    </div>`;
  }).join("");
}

function renderModelComparison(prediction, selectedId) {
  const outputs = state.models.map(release => ({ release, output: modelOutput(prediction, release.id) }));
  const available = outputs.filter(item => item.output?.side);
  const sides = new Set(available.map(item => item.output.side));
  const decisions = new Set(available.map(item => modelDecision(item.output)));
  let consensus = "Waiting for every model to finish this round.";
  if (available.length) {
    if (sides.size > 1) consensus = "Models disagree on the predicted direction. Review before trading.";
    else if (decisions.size > 1) consensus = `Models predict ${available[0].output.side.toUpperCase()}, but disagree on whether the entry qualifies.`;
    else consensus = `All available models agree: ${modelDecision(available[0].output)}.`;
  }
  $("modelConsensus").textContent = consensus;
  $("modelComparison").innerHTML = modelComparisonMarkup(prediction, selectedId);
  $("modelAnalysis").innerHTML = modelAnalysisMarkup(prediction, selectedId);
}

function renderLive() {
  const live = state.live || {};
  const prediction = live.prediction || {};
  const paper = live.trading || live.paper;
  const selectedModel = activeModelId();
  const selectedOutput = modelOutput(prediction, selectedModel);
  const primary = selectedOutput ? { ...prediction, ...selectedOutput,
    model_version: selectedModel, market_stability: prediction.market_stability } : prediction;
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
  $("bankroll").textContent = paper ? fmt.usd(paper.available_bankroll) : "Not running";
  $("activeModelCopy").textContent = `${modelName(selectedModel)} · ${activeSession() ? "controls active session" : "dashboard default"}`;
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
  renderModelComparison(prediction, selectedModel);
  renderPaperControl();
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
    analytic_probability_below_90pct: "Confidence is below the configured signal threshold.",
    analytic_probability_below_87_5pct: "Confidence is below V3's recommended 87.5% threshold.",
    analytic_combined_risk: "BTC is close to the opening price while recent movement disagrees with the predicted side.",
    analytic_regime_risk: "V3 blocked a thin-gap round whose recent price path is choppy and upward.",
    analytic_v4_dual_risk: "V4 blocked a close-to-line round with movement disagreement or a choppy-up price path.",
    dual_risk: "V4's close-to-line risk guard blocked this entry.",
    confidence_below_threshold: "The forecast did not reach this model's trading threshold.",
    adverse_price_move_requires_confirmation: "The contract price moved more than 15¢ against the original signal before a fill was available.",
    regime_risk: "V3 blocked the thin-gap and choppy-up risk pattern.",
    "analytic_disagreement:market": "The settlement estimate and Polymarket direction disagree.",
    analytic_edge_below_threshold: "The available price is too expensive for the required profit margin.",
    analytic_liquidity_unavailable: "There is not enough reliable liquidity at the best price.",
    model_market_disagreement: "This model's predicted side disagrees with Polymarket's direction.",
    historical_probability_only: "This older round recorded the probability, but not the model's complete decision.",
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
  const values = [["Evaluated rounds", s.rounds ?? 0], ["Signals", s.signals ?? 0], ["Signal accuracy", fmt.percent(s.signal_accuracy)], ["Paper trades", s.paper_trades ?? 0], ["Realized P&L", fmt.usd(s.realized_profit)]];
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
  $("historyBody").innerHTML = state.rounds.map(row => `<tr data-slug="${row.slug}"><td><b>${fmt.date(row.start)}</b><br><small class="mono">${row.slug.replace("btc-updown-5m-", "#")}</small></td><td class="${sideClass(row.prediction)}"><b>${row.prediction}</b></td><td class="history-models">${modelComparisonMarkup(row, row.traded_model_version || row.model_version || state.defaultModel, true)}</td><td>${row.actual || "—"}</td><td>${badge(row.result)}</td><td class="mono">${fmt.percent(row.estimated_edge)}</td><td>${row.traded ? `Yes${row.traded_model_version ? `<small>${escapeHtml(modelName(row.traded_model_version))}</small>` : ""}` : "No"}</td></tr>`).join("");
  $("historyEmpty").classList.toggle("hidden", state.rounds.length > 0);
  renderPagination("history");
  prepareMobileTables();
  document.querySelectorAll("#historyBody tr").forEach(row => row.addEventListener("click", () => openRound(row.dataset.slug)));
}

async function openRound(slug) {
  try {
    const detail = await request(`/api/v1/rounds/${slug}`);
    const decision = detail.decision || {};
    const tradedModel = detail.paper_trades?.[0]?.model_version || decision.model_version || state.defaultModel;
    $("roundDetail").innerHTML = `<p class="eyebrow">ROUND INSPECTION</p><h2>${slug}</h2><div class="signal-stats"><div><span>Default prediction</span><b class="${sideClass(decision.direction)}">${decision.direction || "—"}</b></div><div><span>UP probability</span><b>${fmt.percent(decision.p_up)}</b></div><div><span>Result</span><b>${detail.resolution?.actual_result || "Pending"}</b></div></div><div class="detail-section"><h3>MODEL COMPARISON</h3><div class="model-comparison">${modelComparisonMarkup(decision, tradedModel)}</div><div class="round-model-analysis">${modelAnalysisMarkup(decision, tradedModel, detail.resolution?.actual_result)}</div></div><div class="detail-section"><h3>DEFAULT DECISION REASON</h3><p>${humanReason(decision.reason)}</p></div><div class="detail-section"><h3>AGREEMENT</h3><pre class="detail-json"></pre></div><div class="detail-section"><h3>COMPONENTS</h3><pre class="detail-json"></pre></div>`;
    const blocks = $("roundDetail").querySelectorAll("pre");
    blocks[0].textContent = JSON.stringify(decision.agreement || {}, null, 2);
    blocks[1].textContent = JSON.stringify(decision.components || {}, null, 2);
    $("roundDialog").showModal();
  } catch (error) { toast(error.message); }
}

function isTerminalSession(session) {
  return ["STOPPED", "COMPLETED", "GAME_OVER"].includes(session?.status);
}

function modelRelease(modelId) {
  return state.models.find(model => model.id === modelId) || null;
}

function modelName(modelId) {
  return modelRelease(modelId)?.name || (modelId ? modelId : "Unversioned model");
}

function riskRuleText(modelId) {
  const release = modelRelease(modelId);
  if (release?.risk_rule === "thin_gap_and_movement_or_choppy_up") {
    const adverseCents = Math.round(Number(release.maximum_adverse_price_move || .15) * 100);
    return {
      title: "V4 dual protection",
      copy: `A conservative spot-or-TWAP thin gap combined with movement disagreement or choppy-up is skipped. A fill is also rejected after a ${adverseCents}¢ adverse price move.`,
      budget: `Dual risk: SKIP · adverse move: ${adverseCents}¢ max`,
      session: `dual close-to-line risk skipped; ${adverseCents}¢ adverse-move guard`,
    };
  }
  if (release?.risk_rule === "thin_gap_and_choppy_up") {
    return {
      title: "V3 regime protection",
      copy: "A thin gap together with a choppy-up price regime is always skipped. Movement disagreement remains a configurable size warning.",
      budget: "Thin choppy-up: SKIP",
      session: "thin gap with choppy-up skipped",
    };
  }
  return {
    title: "Combined-risk protection",
    copy: "A thin gap together with movement disagreement is always skipped.",
    budget: "Combined risk: SKIP",
    session: "combined risk skipped",
  };
}

function renderModelChoices(selected, applyRecommendation = false) {
  const select = $("sessionModelVersion");
  if (!select || !state.models.length) return;
  const value = selected || select.value || state.defaultModel;
  select.innerHTML = state.models.map(model =>
    `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}${model.default ? " · Recommended" : ""}</option>`
  ).join("");
  select.value = state.models.some(model => model.id === value) ? value : state.defaultModel;
  const release = modelRelease(select.value);
  const recommended = Number(release?.recommended_confidence);
  if (applyRecommendation && Number.isFinite(recommended)) {
    $("paperSessionForm").elements.confidence_percent.value = recommended * 100;
  }
  const recommendation = Number.isFinite(recommended)
    ? ` Recommended paper confidence: ${fmt.percent(recommended, 1)}.` : "";
  $("sessionModelCopy").textContent = (release?.description || "Choose the signal model this session will trade.") + recommendation;
  const rule = riskRuleText(select.value);
  $("sessionRiskRuleTitle").textContent = rule.title;
  $("sessionRiskRuleCopy").textContent = rule.copy;
}

function activeSession() {
  return state.sessions.find(session => !isTerminalSession(session))
    || state.activeSessionSummary || null;
}

function renderPaperControl() {
  const runtime = Boolean(state.dashboard?.capabilities?.paper_session_runtime);
  const active = activeSession();
  const stopping = active?.status === "STOPPING";
  const accepting = Boolean(active?.status_detail?.accepting_entries) && !stopping;
  $("autoTradingToggle").checked = accepting;
  $("autoTradingToggle").disabled = !runtime || stopping;
  $("autoTradingStatus").textContent = !runtime ? "UNAVAILABLE" : active ? active.status.replaceAll("_", " ") : "READY";
  $("autoTradingStatus").className = `pill ${accepting ? "signal" : active ? "warning" : "muted"}`;
  const mode = active?.mode || "paper";
  $("autoTradingCopy").textContent = !runtime
    ? "Trading sessions require the collector runtime."
    : active
      ? `${mode.toUpperCase()} · ${active.name} · ${fmt.usd(active.status_detail?.available_bankroll)} available · entries stop at ${fmt.num(active.configuration.entry_cutoff_seconds, 0)} seconds.`
      : "Create a paper or real session, set its balance-based limits, then let the bot monitor entries.";
}

function renderSessionList() {
  state.sessions.sort((a, b) => Number(b.created_ms || 0) - Number(a.created_ms || 0));
  $("sessionsEmpty").classList.toggle("hidden", state.sessions.length > 0);
  $("sessionList").innerHTML = state.sessions.map(session => `<button class="session-item ${session.session_id === state.selectedSessionId ? "active" : ""}" data-session-id="${session.session_id}"><div class="session-item-head"><strong>${escapeHtml(session.name)}</strong>${badge(session.status)}</div><small>${String(session.mode || "paper").toUpperCase()} · ${escapeHtml(modelName(session.configuration?.model_version))} · ${new Date(session.created_ms).toLocaleString()}</small><div class="session-item-stats"><span>BALANCE<b>${fmt.usd(session.status_detail?.available_bankroll)}</b></span><span>TRADES<b>${session.trade_count}</b></span><span>P&amp;L<b class="${session.realized_profit >= 0 ? "positive" : "negative"}">${fmt.usd(session.realized_profit)}</b></span></div></button>`).join("");
  document.querySelectorAll(".session-item").forEach(button => button.addEventListener("click", () => selectPaperSession(button.dataset.sessionId)));
  renderPaperControl();
}

function renderSessionDetail() {
  const session = state.sessionDetail;
  const trades = session?.trades || [];
  state.trades = trades;
  if (!session) {
    $("sessionLedgerTitle").textContent = "Select a session";
    $("sessionRules").textContent = "";
    $("tradeMetrics").innerHTML = "";
    $("editSessionButton").classList.add("hidden");
  } else {
    const config = session.configuration;
    $("sessionLedgerTitle").textContent = session.name;
    const stopRules = [config.max_trades ? `${config.max_trades} trades` : "manual stop",
      config.target_profit_usd ? `${fmt.usd(config.target_profit_usd)} profit target` : null,
      config.max_loss_usd ? `${fmt.usd(config.max_loss_usd)} loss limit` : null].filter(Boolean);
    const allocationRule = session.legacy ? "legacy 80% / below-$20 rule" : `${fmt.percent(config.allocation_fraction, 0)} of balance per trade`;
    const modelRisk = riskRuleText(config.model_version).session;
    const riskRules = session.legacy ? "legacy risk sizing" : `thin gap ${fmt.percent(config.thin_gap_allocation_fraction ?? .25, 0)} · movement warning ${fmt.percent(config.movement_disagreement_allocation_fraction ?? .5, 0)} · ${modelRisk}`;
    $("sessionRules").textContent = `${String(session.mode || "paper").toUpperCase()} · ${modelName(config.model_version)} · ${fmt.percent(config.model_threshold)} confidence · ${fmt.percent(config.minimum_edge)} edge · ${allocationRule} · ${riskRules} · stop entries at ${fmt.num(config.entry_cutoff_seconds, 0)}s · ${stopRules.join(" · ")}`;
    const values = [["Starting", fmt.usd(config.starting_bankroll)], ["Available", fmt.usd(session.status_detail?.available_bankroll)], ["Trades", session.trade_count], ["Wins / losses", `${session.wins} / ${session.losses}`], ["Realized P&L", fmt.usd(session.realized_profit)]];
    $("tradeMetrics").innerHTML = values.map(([label,value]) => `<div class="metric"><span>${label}</span><b>${value}</b></div>`).join("");
    const editable = activeSession()?.session_id === session.session_id && !isTerminalSession(session);
    $("editSessionButton").classList.toggle("hidden", !editable);
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
  if (event.event === "run_started") return `Started in ${(event.execution_mode || "paper").toUpperCase()} mode with ${fmt.usd(event.starting_bankroll)} · ${modelName(event.model_version)}`;
  if (event.event === "order_preflight_rejected") return `${round} · exchange preflight: ${event.message || "rejected"}`;
  if (event.event === "order_submission_uncertain") return `${round} · submission result uncertain; retry suppressed to prevent a duplicate order`;
  if (event.event === "order_rejected") return `${round} · Polymarket rejected the FOK order`;
  if (event.event === "configuration_updated") return `Runtime rules updated · next budget ${fmt.usd(event.planned_allocation)}`;
  if (event.event === "signal_rejected") return `${round || "Signal"} · ${String(event.reason || "risk rule").replaceAll("_", " ")}`;
  return round || String(event.reason || "Session state updated").replaceAll("_", " ");
}

async function selectPaperSession(sessionId) {
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

async function loadPaperSessions() {
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
    return loadPaperSessions();
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

function renderSettings() {
  const values = state.settings || {};
  const form = $("settingsForm");
  Object.entries(values).forEach(([key,value]) => { const input = form.elements[key]; if (!input) return; input.type === "checkbox" ? input.checked = value : input.value = value; });
  const c = state.dashboard?.capabilities || {};
  $("capabilityList").innerHTML = [["Paper sessions", c.paper_session_runtime], ["Model races", c.model_races], ["Automatic real trading", c.automatic_real_trading], ["Manual real orders", c.manual_orders], ["Live WebSocket", c.live_websocket]].map(([name,on]) => `<div><span>${name}</span><b>${on ? "AVAILABLE" : "DISABLED"}</b></div>`).join("");
  renderPaperControl();
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const fields = ["model_confidence_threshold", "minimum_edge", "entry_stop_remaining_seconds", "minimum_liquidity_shares", "max_trades_per_day", "max_trade_usd", "max_daily_loss_usd"];
  const payload = Object.fromEntries(fields.map(key => [key, Number(form.elements[key].value)]));
  try {
    const result = await request("/api/v1/settings", { method: "PATCH", body: JSON.stringify(payload) });
    state.settings = result.values; renderSettings();
    $("settingsMessage").textContent = "Defaults saved for future sessions. Active-session rules stay fixed.";
    toast("Strategy saved");
  } catch (error) { $("settingsMessage").textContent = error.message; }
}

async function toggleAutomaticTrading(event) {
  const toggle = event.currentTarget;
  const active = activeSession();
  if (toggle.checked && !active) {
    openPaperSessionDialog();
    return;
  }
  if (!toggle.checked && active) {
    toggle.disabled = true;
    try {
      await request(`/api/v1/trading/sessions/${encodeURIComponent(active.session_id)}/stop`, { method: "POST" });
      active.status = "STOPPING";
      active.status_detail.accepting_entries = false;
      renderSessionList();
      toast(`${String(active.mode || "paper").toUpperCase()} session is stopping`);
    } catch (error) { toast(error.message); await loadPaperSessions(); }
  }
}

function optionalFormNumber(form, name) {
  const value = form.elements[name].value.trim();
  return value === "" ? null : Number(value);
}

function executableBudget(bankroll, fraction, cap, multiplier = 1) {
  let base = bankroll * fraction;
  if (cap != null && Number.isFinite(cap)) base = Math.min(base, cap);
  if (!Number.isFinite(base) || base <= 0 || multiplier <= 0) return { amount: 0, skipped: true, floored: false };
  const raw = base * multiplier;
  if (raw < 1) {
    const canFloor = bankroll >= 1 && (cap == null || cap >= 1);
    return canFloor
      ? { amount: 1, skipped: false, floored: true }
      : { amount: 0, skipped: true, floored: false };
  }
  return { amount: Math.min(bankroll, raw), skipped: false, floored: false };
}

function riskBudgetCopy(bankroll, fraction, cap, thinFraction, movementFraction, modelId) {
  const describe = (label, multiplier) => {
    const budget = executableBudget(bankroll, fraction, cap, multiplier);
    if (multiplier <= 0 || budget.skipped) return `${label}: SKIP`;
    return `${label}: ${fmt.usd(budget.amount)}${budget.floored ? " (raised to $1 minimum)" : ""}`;
  };
  return `${describe("Thin gap", thinFraction)} · ${describe("Movement warning", movementFraction)} · ${riskRuleText(modelId).budget} · fees separate`;
}

function syncSessionMode() {
  const form = $("paperSessionForm");
  const real = form.elements.mode.value === "real";
  const cash = Number(state.account?.balance_usdc);
  const accountReady = Boolean(state.account?.connected
                               && Number.isFinite(cash) && cash >= 1);
  const bankroll = form.elements.starting_bankroll;
  bankroll.readOnly = real;
  bankroll.required = !real;
  if (real) bankroll.value = Number.isFinite(cash) ? cash.toFixed(2) : "";
  $("startingBankrollCopy").textContent = real
    ? "Live available USDC from your connected Polymarket account."
    : "Simulated money for this session.";
  $("sessionModeCopy").textContent = real
    ? "Qualified signals submit bounded fill-or-kill orders automatically. There is no confirmation for each trade after activation."
    : "Uses a simulated bankroll and never submits an exchange order.";
  $("startSessionButton").textContent = real ? "Activate real-money bot" : "Start paper session";
  $("startSessionButton").disabled = real && !accountReady;
  for (const name of ["maximum_trade_usd", "max_loss_usd"]) {
    const input = form.elements[name];
    if (real && Number.isFinite(cash)) {
      input.max = String(cash);
      if (input.value && Number(input.value) > cash) input.value = cash.toFixed(2);
    } else {
      input.removeAttribute("max");
    }
  }
  form.elements.maximum_trade_usd.min = real ? "1" : "0.01";
  if (real && form.elements.maximum_trade_usd.value
      && Number(form.elements.maximum_trade_usd.value) < 1) {
    form.elements.maximum_trade_usd.value = "1.00";
  }
  if (real && !accountReady) {
    $("sessionFormMessage").textContent = state.account?.error ||
      "Connect a Polymarket account with at least $1 available before activating real mode.";
  } else if ($("sessionFormMessage").textContent.includes("Polymarket account")) {
    $("sessionFormMessage").textContent = "";
  }
  updateSessionBudgetPreview();
}

function updateSessionBudgetPreview() {
  const form = $("paperSessionForm");
  const bankroll = form.elements.mode.value === "real"
    ? Number(state.account?.balance_usdc) : Number(form.elements.starting_bankroll.value);
  const fraction = Number(form.elements.allocation_percent.value) / 100;
  const cap = optionalFormNumber(form, "maximum_trade_usd");
  const normal = executableBudget(bankroll, fraction, cap);
  $("sessionBudgetPreview").textContent = normal.skipped ? "NOT TRADABLE" : `${fmt.usd(normal.amount)}${normal.floored ? " minimum" : ""}`;
  $("sessionRiskBudgetPreview").textContent = riskBudgetCopy(
    bankroll, fraction, cap,
    Number(form.elements.thin_gap_allocation_percent.value) / 100,
    Number(form.elements.movement_disagreement_allocation_percent.value) / 100,
    form.elements.model_version.value);
}

function updateEditSessionBudgetPreview() {
  const form = $("paperSessionEditForm");
  const bankroll = state.sessionDetail?.mode === "real"
    ? Number(state.account?.balance_usdc)
    : Number(state.sessionDetail?.status_detail?.available_bankroll);
  const fraction = Number(form.elements.allocation_percent.value) / 100;
  const cap = optionalFormNumber(form, "maximum_trade_usd");
  const normal = executableBudget(bankroll, fraction, cap);
  $("editSessionBudgetPreview").textContent = normal.skipped ? "NOT TRADABLE" : `${fmt.usd(normal.amount)}${normal.floored ? " minimum" : ""}`;
  $("editSessionRiskBudgetPreview").textContent = riskBudgetCopy(
    bankroll, fraction, cap,
    Number(form.elements.thin_gap_allocation_percent.value) / 100,
    Number(form.elements.movement_disagreement_allocation_percent.value) / 100,
    state.sessionDetail?.configuration?.model_version);
}

function openEditSessionDialog() {
  const session = state.sessionDetail;
  if (!session || activeSession()?.session_id !== session.session_id || isTerminalSession(session)) {
    toast("Only the active session can be edited");
    return;
  }
  const config = session.configuration;
  const form = $("paperSessionEditForm");
  form.elements.name.value = session.name || "";
  form.elements.allocation_percent.value = Number(config.allocation_fraction) * 100;
  form.elements.maximum_trade_usd.value = config.maximum_trade_usd ?? "";
  form.elements.confidence_percent.value = Number(config.model_threshold) * 100;
  form.elements.minimum_edge_percent.value = Number(config.minimum_edge) * 100;
  form.elements.entry_cutoff_seconds.value = config.entry_cutoff_seconds;
  form.elements.thin_gap_allocation_percent.value = Number(config.thin_gap_allocation_fraction ?? .25) * 100;
  form.elements.movement_disagreement_allocation_percent.value = Number(config.movement_disagreement_allocation_fraction ?? .5) * 100;
  form.elements.max_trades.value = config.max_trades ?? "";
  form.elements.target_profit_usd.value = config.target_profit_usd ?? "";
  form.elements.max_loss_usd.value = config.max_loss_usd ?? "";
  if (session.mode === "real") {
    const cash = Number(state.account?.balance_usdc);
    if (Number.isFinite(cash)) {
      form.elements.maximum_trade_usd.max = String(cash);
      form.elements.max_loss_usd.max = String(cash);
    }
    form.elements.maximum_trade_usd.min = "1";
  } else {
    form.elements.maximum_trade_usd.removeAttribute("max");
    form.elements.max_loss_usd.removeAttribute("max");
    form.elements.maximum_trade_usd.min = "0.01";
  }
  $("editSessionFormMessage").textContent = "";
  const rule = riskRuleText(config.model_version);
  $("editSessionRiskRuleTitle").textContent = rule.title;
  $("editSessionRiskRuleCopy").textContent = rule.copy;
  updateEditSessionBudgetPreview();
  $("paperSessionEditDialog").showModal();
}

async function updatePaperSession(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  const sessionId = state.sessionDetail?.session_id;
  if (!sessionId) return;
  const payload = {
    name: form.elements.name.value.trim() || null,
    allocation_percent: Number(form.elements.allocation_percent.value),
    maximum_trade_usd: optionalFormNumber(form, "maximum_trade_usd"),
    confidence_percent: Number(form.elements.confidence_percent.value),
    minimum_edge_percent: Number(form.elements.minimum_edge_percent.value),
    entry_cutoff_seconds: Number(form.elements.entry_cutoff_seconds.value),
    thin_gap_allocation_percent: Number(form.elements.thin_gap_allocation_percent.value),
    movement_disagreement_allocation_percent: Number(form.elements.movement_disagreement_allocation_percent.value),
    max_trades: optionalFormNumber(form, "max_trades"),
    target_profit_usd: optionalFormNumber(form, "target_profit_usd"),
    max_loss_usd: optionalFormNumber(form, "max_loss_usd"),
  };
  submit.disabled = true;
  $("editSessionFormMessage").textContent = "";
  try {
    await request(`/api/v1/trading/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH", body: JSON.stringify(payload),
    });
    $("paperSessionEditDialog").close();
    await loadPaperSessions();
    toast("Runtime configuration updated");
  } catch (error) {
    $("editSessionFormMessage").textContent = error.message;
  } finally { submit.disabled = false; }
}

function openPaperSessionDialog() {
  if (activeSession()) {
    toast("Stop the active session before creating another one");
    renderPaperControl();
    return;
  }
  if (!state.dashboard?.capabilities?.paper_session_runtime) {
    toast("Trading-session runtime is unavailable");
    return;
  }
  const form = $("paperSessionForm");
  form.reset();
  form.elements.mode.value = "paper";
  const defaults = state.settings || {};
  form.elements.allocation_percent.value = Number(defaults.allocation_fraction_above_threshold ?? .8) * 100;
  form.elements.confidence_percent.value = Number(defaults.model_confidence_threshold ?? .9) * 100;
  form.elements.minimum_edge_percent.value = Number(defaults.minimum_edge ?? .03) * 100;
  form.elements.entry_cutoff_seconds.value = Number(defaults.entry_stop_remaining_seconds ?? 30);
  form.elements.thin_gap_allocation_percent.value = 25;
  form.elements.movement_disagreement_allocation_percent.value = 50;
  form.elements.maximum_trade_usd.value = defaults.max_trade_usd ?? "";
  form.elements.max_trades.value = defaults.max_trades_per_day ?? "";
  form.elements.max_loss_usd.value = defaults.max_daily_loss_usd ?? "";
  renderModelChoices(state.defaultModel, true);
  $("sessionFormMessage").textContent = "";
  syncSessionMode();
  updateSessionBudgetPreview();
  $("paperSessionDialog").showModal();
}

async function startPaperSession(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  const payload = {
    mode: form.elements.mode.value,
    model_version: form.elements.model_version.value,
    name: form.elements.name.value.trim() || null,
    starting_bankroll: form.elements.mode.value === "paper"
      ? Number(form.elements.starting_bankroll.value) : null,
    allocation_percent: Number(form.elements.allocation_percent.value),
    maximum_trade_usd: optionalFormNumber(form, "maximum_trade_usd"),
    confidence_percent: Number(form.elements.confidence_percent.value),
    minimum_edge_percent: Number(form.elements.minimum_edge_percent.value),
    entry_cutoff_seconds: Number(form.elements.entry_cutoff_seconds.value),
    thin_gap_allocation_percent: Number(form.elements.thin_gap_allocation_percent.value),
    movement_disagreement_allocation_percent: Number(form.elements.movement_disagreement_allocation_percent.value),
    max_trades: optionalFormNumber(form, "max_trades"),
    target_profit_usd: optionalFormNumber(form, "target_profit_usd"),
    max_loss_usd: optionalFormNumber(form, "max_loss_usd"),
  };
  submit.disabled = true;
  $("sessionFormMessage").textContent = "";
  try {
    const session = await request("/api/v1/trading/sessions", {
      method: "POST", body: JSON.stringify(payload),
    });
    state.selectedSessionId = session.session_id;
    $("paperSessionDialog").close();
    await loadPaperSessions();
    toast(`${String(session.mode || "paper").toUpperCase()} session started`);
  } catch (error) {
    $("sessionFormMessage").textContent = error.message;
  } finally {
    submit.disabled = false;
    if (form.elements.mode.value === "real") syncSessionMode();
  }
}

function raceModelCard(model) {
  const status = model.status || {};
  const decided = Number(model.wins || 0) + Number(model.losses || 0);
  const accuracy = decided ? Number(model.wins) / decided : null;
  const pnl = Number(status.realized_profit || 0);
  return `<article class="card race-model-card ${model.model_version === "btc_guard_v4" ? "featured" : ""}">
    <div class="race-model-title"><div><span>${escapeHtml(model.name)}</span><small>${escapeHtml(model.model_version)}</small></div>${badge(status.status || "WAITING")}</div>
    <div class="race-balance"><span>AVAILABLE BALANCE</span><strong>${fmt.usd(status.available_bankroll)}</strong><em class="${pnl >= 0 ? "positive" : "negative"}">${pnl >= 0 ? "+" : ""}${fmt.usd(pnl)}</em></div>
    <div class="race-card-stats"><span>Trades<b>${model.trades || 0}</b></span><span>W / L<b>${model.wins || 0} / ${model.losses || 0}</b></span><span>Accuracy<b>${fmt.percent(accuracy)}</b></span><span>Missed wins<b>${model.missed_wins || 0}</b></span></div>
    <p>${fmt.percent(model.confidence)} threshold · ${escapeHtml(riskRuleText(model.model_version).session)}</p>
  </article>`;
}

function renderRaceRules() {
  $("raceModelRules").innerHTML = state.models.map(model => `<div class="race-rule-row"><div><strong>${escapeHtml(model.name)}</strong><small>${escapeHtml(model.description || "")}</small></div><span>${fmt.percent(model.recommended_confidence)}</span><p>${escapeHtml(riskRuleText(model.id).copy)}</p></div>`).join("");
  const filter = $("raceModelFilter"), selected = filter.value;
  filter.innerHTML = `<option value="">All models</option>` + state.models.map(model => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join("");
  filter.value = selected;
}

function raceRoundRows() {
  if (!state.raceDetail) return [];
  return state.raceDetail.models.flatMap(model => (model.rounds || []).map(round => ({ ...round, model_version: model.model_version, model_name: model.name })));
}

function renderModelRace() {
  renderRaceRules();
  const race = state.raceDetail;
  const active = race?.status === "RUNNING";
  $("modelRaceStatus").textContent = race?.status || "READY";
  $("modelRaceStatus").className = `pill ${active ? "signal" : "muted"}`;
  $("startModelRaceButton").disabled = active;
  $("stopModelRaceButton").classList.toggle("hidden", !active);
  $("raceLiveCards").innerHTML = race ? race.models.map(raceModelCard).join("") : `<div class="empty-state">No model race is selected.</div>`;
  const rows = raceRoundRows().sort((a, b) => Number(b.start || 0) - Number(a.start || 0)).slice(0, 24);
  $("raceLiveBody").innerHTML = rows.map(row => `<tr><td>${escapeHtml(row.slug?.replace("btc-updown-5m-", "#") || "—")}</td><td><b>${escapeHtml(row.model_name)}</b></td><td class="${sideClass(row.side)}">${row.side || "—"} ${fmt.percent(row.probability)}</td><td>${badge(row.state)}</td><td>${fmt.cents(row.entry_price)}</td><td>${fmt.usd(row.cost)}</td><td class="${Number(row.profit || 0) >= 0 ? "positive" : "negative"}">${fmt.usd(row.profit)}</td></tr>`).join("");
  prepareMobileTables();
}

function renderRacePerformance() {
  const selector = $("racePerformanceSelector"), selected = state.selectedRaceId;
  selector.innerHTML = state.modelRaces.length ? state.modelRaces.map(race => `<option value="${escapeHtml(race.race_id)}">${escapeHtml(race.name)} · ${new Date(race.created_ms).toLocaleString()}</option>`).join("") : `<option value="">No races yet</option>`;
  if (selected) selector.value = selected;
  const race = state.raceDetail;
  $("racePerformanceCards").innerHTML = race ? race.models.map(raceModelCard).join("") : `<div class="empty-state">Start a model race to begin the forward comparison.</div>`;
  const modelFilter = $("raceModelFilter").value, stateFilter = $("raceStateFilter").value, opportunityFilter = $("raceOpportunityFilter").value;
  const matchingRows = raceRoundRows().filter(row => (!modelFilter || row.model_version === modelFilter) && (!stateFilter || row.state === stateFilter) && (!opportunityFilter || row.opportunity === opportunityFilter)).sort((a, b) => Number(b.start || 0) - Number(a.start || 0));
  const page = state.pages.race;
  page.total = matchingRows.length;
  if (page.offset >= page.total && page.total) page.offset = Math.floor((page.total - 1) / page.limit) * page.limit;
  const rows = matchingRows.slice(page.offset, page.offset + page.limit);
  $("racePerformanceBody").innerHTML = rows.map(row => {
    const warnings = [...(row.warnings || [])];
    if (row.regime) warnings.push(row.regime);
    const gap = row.spot_gap_bps == null ? "—" : `${fmt.num(row.spot_gap_bps, 1)} spot · ${fmt.num(row.twap_gap_bps, 1)} TWAP`;
    const execution = row.opportunity ? row.opportunity.replaceAll("_", " ") : row.entry_price != null ? `${fmt.cents(row.entry_price)} · ${fmt.usd(row.cost)}` : humanReason(row.execution_reason || row.reason);
    return `<tr><td>${escapeHtml(row.slug?.replace("btc-updown-5m-", "#") || "—")}<small>${escapeHtml(row.model_name)}</small></td><td class="${sideClass(row.side)}"><b>${row.side || "—"}</b> ${fmt.percent(row.probability)}</td><td>${row.decision || "SKIP"}<small>${escapeHtml(humanReason(row.reason))}</small></td><td>${warnings.length ? warnings.map(value => `<span class="warning-chip">${escapeHtml(String(value).replaceAll("_", " "))}</span>`).join(" ") : "—"}</td><td class="mono">${gap}</td><td>${row.actual || "Pending"}</td><td>${escapeHtml(execution)}</td><td class="${Number(row.profit || 0) >= 0 ? "positive" : "negative"}">${fmt.usd(row.profit)}</td></tr>`;
  }).join("");
  $("racePerformanceEmpty").classList.toggle("hidden", rows.length > 0);
  renderPagination("race");
  prepareMobileTables();
}

async function loadModelRaces() {
  if (!state.dashboard?.capabilities?.model_races) return;
  const result = await request("/api/v1/model-races");
  state.modelRaces = result.items || [];
  if (!state.selectedRaceId || !state.modelRaces.some(race => race.race_id === state.selectedRaceId)) state.selectedRaceId = state.modelRaces[0]?.race_id || null;
  state.raceDetail = state.selectedRaceId ? await request(`/api/v1/model-races/${encodeURIComponent(state.selectedRaceId)}`) : null;
  renderModelRace();
  renderRacePerformance();
}

async function startModelRace(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    name: form.elements.name.value || null,
    starting_bankroll: Number(form.elements.starting_bankroll.value),
    allocation_percent: Number(form.elements.allocation_percent.value),
    maximum_trade_usd: optionalFormNumber(form, "maximum_trade_usd"),
    minimum_edge_percent: Number(form.elements.minimum_edge_percent.value),
    entry_cutoff_seconds: Number(form.elements.entry_cutoff_seconds.value),
    thin_gap_allocation_percent: Number(form.elements.thin_gap_allocation_percent.value),
    movement_disagreement_allocation_percent: Number(form.elements.movement_disagreement_allocation_percent.value),
  };
  $("startModelRaceButton").disabled = true;
  try {
    const race = await request("/api/v1/model-races", { method: "POST", body: JSON.stringify(payload) });
    state.selectedRaceId = race.race_id;
    $("modelRaceMessage").textContent = "All models started with equal money. Their balances now compound independently.";
    await loadModelRaces();
    toast("Model race started");
  } catch (error) { $("modelRaceMessage").textContent = error.message; }
  finally { if (state.raceDetail?.status !== "RUNNING") $("startModelRaceButton").disabled = false; }
}

async function stopModelRace() {
  if (!state.selectedRaceId) return;
  try {
    await request(`/api/v1/model-races/${encodeURIComponent(state.selectedRaceId)}/stop`, { method: "POST" });
    $("modelRaceMessage").textContent = "Stop requested. Open positions will still be resolved and recorded.";
    await loadModelRaces();
  } catch (error) { toast(error.message); }
}

async function refresh() {
  try {
    const [dashboard, chart, health, account] = await Promise.all([request("/api/v1/dashboard"), request("/api/v1/live/chart"), request("/health"), request("/api/v1/account")]);
    state.dashboard = dashboard; state.live = dashboard.live; state.chart = chart; state.health = health; state.settings = dashboard.settings; state.account = account;
    state.models = dashboard.models || state.models;
    state.defaultModel = dashboard.default_model || state.defaultModel;
    renderModelChoices();
    $("collectorStatus").textContent = health.collector.fresh ? "Live" : health.collector.available ? "Stale" : "Offline";
    $("collectorStatus").className = health.collector.fresh ? "positive" : "negative";
    renderLive(); renderCharts(); renderStatistics(); renderSettings(); renderAccount();
    const followups = [loadPaperSessions()];
    if ($("historyView").classList.contains("active")) followups.push(loadRounds());
    if ($("modelPerformanceView").classList.contains("active")
        || $("modelRaceView").classList.contains("active")) followups.push(loadModelRaces());
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
      Promise.all([loadRounds(), loadPaperSessions(), request("/api/v1/statistics")]).then(([, , statistics]) => {
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
    if ($("paperSessionDialog").open && $("paperSessionForm").elements.mode.value === "real") {
      syncSessionMode();
    }
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
  const titles = { dashboard: ["LIVE OPERATIONS", "Market overview"], history: ["PERFORMANCE", "Round history"], trades: ["BOT EXECUTION", "Trade ledger"], modelPerformance: ["MODEL LAB", "Forward model performance"], modelRace: ["MODEL LAB", "Equal-bankroll paper race"], settings: ["CONFIGURATION", "Strategy settings"] };
  [$("viewEyebrow").textContent, $("viewTitle").textContent] = titles[name];
  history.replaceState(null, "", `#${name}`);
  if (name === "history") loadRounds().catch(error => toast(error.message));
  if (name === "trades") loadPaperSessions().catch(error => toast(error.message));
  if (name === "modelPerformance" || name === "modelRace") {
    loadModelRaces().catch(error => toast(error.message));
  }
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
$("sessionsPrevious").addEventListener("click", () => movePage("sessions", -1, loadPaperSessions));
$("sessionsNext").addEventListener("click", () => movePage("sessions", 1, loadPaperSessions));
$("tradesPrevious").addEventListener("click", () => movePage("trades", -1, reloadSelectedSession));
$("tradesNext").addEventListener("click", () => movePage("trades", 1, reloadSelectedSession));
$("eventsPrevious").addEventListener("click", () => movePage("events", -1, reloadSelectedSession));
$("eventsNext").addEventListener("click", () => movePage("events", 1, reloadSelectedSession));
$("racePrevious").addEventListener("click", () => { movePage("race", -1, async () => renderRacePerformance()); });
$("raceNext").addEventListener("click", () => { movePage("race", 1, async () => renderRacePerformance()); });
$("settingsForm").addEventListener("submit", saveSettings);
$("modelRaceForm").addEventListener("submit", startModelRace);
$("stopModelRaceButton").addEventListener("click", stopModelRace);
$("openRacePerformanceButton").addEventListener("click", () => changeView("modelPerformance"));
$("racePerformanceSelector").addEventListener("change", async event => { state.selectedRaceId = event.target.value; state.pages.race.offset = 0; await loadModelRaces(); });
[$("raceModelFilter"), $("raceStateFilter"), $("raceOpportunityFilter")].forEach(input => input.addEventListener("change", () => { state.pages.race.offset = 0; renderRacePerformance(); }));
$("autoTradingToggle").addEventListener("change", toggleAutomaticTrading);
$("newSessionButton").addEventListener("click", openPaperSessionDialog);
$("paperSessionForm").addEventListener("submit", startPaperSession);
$("paperSessionForm").addEventListener("input", updateSessionBudgetPreview);
$("sessionModelVersion").addEventListener("change", () => {
  renderModelChoices($("sessionModelVersion").value, true);
  updateSessionBudgetPreview();
});
document.querySelectorAll('#paperSessionForm input[name="mode"]').forEach(
  input => input.addEventListener("change", syncSessionMode));
$("sessionDialogClose").addEventListener("click", () => { $("paperSessionDialog").close(); renderPaperControl(); });
$("paperSessionDialog").addEventListener("close", renderPaperControl);
$("editSessionButton").addEventListener("click", openEditSessionDialog);
$("paperSessionEditForm").addEventListener("submit", updatePaperSession);
$("paperSessionEditForm").addEventListener("input", updateEditSessionBudgetPreview);
$("editSessionDialogClose").addEventListener("click", () => $("paperSessionEditDialog").close());
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
  if (state.dashboard?.capabilities?.paper_session_runtime) {
    if (!sessionPollInFlight) {
      sessionPollInFlight = true;
      try { await loadPaperSessions(); } catch (_) {} finally { sessionPollInFlight = false; }
    }
  }
  const raceViewActive = $("modelPerformanceView").classList.contains("active")
    || $("modelRaceView").classList.contains("active");
  if (raceViewActive && state.dashboard?.capabilities?.model_races && !racePollInFlight) {
    racePollInFlight = true;
    try { await loadModelRaces(); } catch (_) {} finally { racePollInFlight = false; }
  }
}, 5000);

const initialView = location.hash.slice(1);
if (["history", "trades", "modelPerformance", "modelRace", "settings"].includes(initialView)) changeView(initialView);
refresh();
connectSockets();
