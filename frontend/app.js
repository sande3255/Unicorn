// UNICORN demo frontend — vanilla JS, no build step, no external dependencies.

const state = {
  token: localStorage.getItem('pm_token') || null,
  user: null, // { username, balance, is_admin }
};

// Mirrors DEPOSIT_FEE_PCT / DEPOSIT_FEE_FLAT in server.py — cosmetic label
// only, the real numbers are enforced server-side.
const DEPOSIT_FEE_LABEL = '3% + $0.30';

// ---------- markets list filter state (search box + category chips) ----------
// status defaults to 'open': the timed-market roster churns every 5-15 min,
// so an unfiltered fetch only grows forever the longer an instance stays up
// (see /api/markets' docstring in server.py) — 'open' keeps the default
// view roughly bounded at the live roster size instead of every market ever
// created. The Resolved/All toggle below lets someone opt into the rest.
const marketsFilterState = { search: '', category: 'All', status: 'open' };
let lastFetchedMarkets = [];

const appEl = document.getElementById('app');
const navEl = document.getElementById('main-nav');
const authAreaEl = document.getElementById('auth-area');

const COIN_SVG = `<svg class="coin" width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <circle cx="12" cy="12" r="9.5" fill="currentColor"/>
  <circle cx="12" cy="12" r="9.5" stroke="#0a0900" stroke-opacity="0.35" stroke-width="1"/>
  <circle cx="12" cy="12" r="6.3" fill="none" stroke="#0a0900" stroke-opacity="0.35" stroke-width="1"/>
  <text x="12" y="16" text-anchor="middle" font-size="9" font-weight="700" fill="#0a0900" font-family="system-ui, sans-serif">$</text>
</svg>`;

// ---------- interval bookkeeping (countdown timers / polling) ----------
// Cleared on every route change so timers don't pile up as you navigate.
let activeIntervals = [];
function trackInterval(id) { activeIntervals.push(id); return id; }
function clearTrackedIntervals() { activeIntervals.forEach(clearInterval); activeIntervals = []; }

function secondsRemaining(closeIso) {
  if (!closeIso) return null;
  const closeMs = new Date(closeIso.endsWith('Z') ? closeIso : closeIso + 'Z').getTime();
  return Math.floor((closeMs - Date.now()) / 1000);
}

function fmtCountdown(closeIso) {
  const sec = secondsRemaining(closeIso);
  if (sec === null) return null;
  if (sec <= 0) return 'closing…';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// Neon-blue at rest, escalating to an urgent pulsing red as a market's
// close time approaches — so "time's almost up" reads at a glance instead
// of requiring someone to actually parse the digits.
function countdownUrgencyClass(closeIso) {
  const sec = secondsRemaining(closeIso);
  if (sec === null) return '';
  if (sec <= 10) return 'countdown countdown-critical';
  if (sec <= 60) return 'countdown countdown-warn';
  return 'countdown countdown-calm';
}

function fmtUnderlyingPrice(p) {
  if (p === null || p === undefined) return '—';
  return p < 1 ? `$${p.toFixed(4)}` : `$${p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// ---------- API helper ----------

async function api(path, { method = 'GET', body = null, auth = true } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (auth && state.token) headers['Authorization'] = `Bearer ${state.token}`;
  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const msg = (data && data.detail) ? data.detail : `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtMoney(n) {
  return '$' + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPct(p) {
  return Math.round(p * 100) + '%';
}

function fmtTime(iso) {
  try {
    return new Date(iso + 'Z').toLocaleString();
  } catch (e) { return iso; }
}

// ---------- session bootstrap ----------

async function refreshMe() {
  if (!state.token) { state.user = null; return; }
  try {
    state.user = await api('/api/me');
  } catch (e) {
    state.token = null;
    state.user = null;
    localStorage.removeItem('pm_token');
  }
}

// ---------- wallet connect (MetaMask / any injected EIP-1193 wallet) ----------
// Login-only: this proves you control an address (by signing a one-time
// challenge) and links or authenticates an account with it. No real
// crypto ever moves — see API.md / README for why UNICORN doesn't do
// real deposits.

async function connectWalletAndSign() {
  if (!window.ethereum) {
    throw new Error('No wallet found in this browser — install MetaMask (or another browser wallet extension) and try again.');
  }
  const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
  const address = (accounts && accounts[0]) || null;
  if (!address) {
    throw new Error('No wallet account was returned — check your wallet extension is unlocked.');
  }
  const { message } = await api('/api/wallet/nonce', { method: 'POST', auth: false, body: { address } });
  const signature = await window.ethereum.request({
    method: 'personal_sign',
    params: [message, address],
  });
  return { address, signature };
}

function shortAddress(addr) {
  if (!addr) return '';
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function logout() {
  state.token = null;
  state.user = null;
  localStorage.removeItem('pm_token');
  renderHeader();
  navigate('#/markets');
}

// ---------- header ----------

function renderHeader() {
  const route = location.hash || '#/markets';
  const links = [
    ['#/markets', 'Markets'],
    ['#/leaderboard', 'Leaderboard'],
  ];
  if (state.user) links.push(['#/portfolio', 'Portfolio']);
  if (state.user) links.push(['#/history', 'History']);
  if (state.user) links.push(['#/api-keys', 'API keys']);
  if (state.user) links.push(['#/account', 'Account']);
  if (state.user && state.user.is_admin) links.push(['#/admin', 'Admin']);

  navEl.innerHTML = links.map(([href, label]) => {
    const active = route.startsWith(href) ? ' class="active"' : '';
    return `<a href="${href}"${active}>${label}</a>`;
  }).join('');

  if (state.user) {
    authAreaEl.innerHTML = `
      <span class="balance-pill">${COIN_SVG}${fmtMoney(state.user.balance)}</span>
      <span class="muted">${escapeHtml(state.user.username)}</span>
      <button id="logout-btn">Log out</button>
    `;
    document.getElementById('logout-btn').onclick = logout;
  } else {
    authAreaEl.innerHTML = `<a href="#/login" class="btn">Log in / Sign up</a>`;
  }
}

// ---------- router ----------

const routes = [
  { pattern: /^#\/markets$/, handler: renderMarketsList, title: 'Markets' },
  { pattern: /^#\/market\/(\d+)$/, handler: renderMarketDetail }, // sets its own title once the question loads
  { pattern: /^#\/portfolio$/, handler: renderPortfolio, auth: true, title: 'Portfolio' },
  { pattern: /^#\/leaderboard$/, handler: renderLeaderboard, title: 'Leaderboard' },
  { pattern: /^#\/history$/, handler: renderHistory, auth: true, title: 'History' },
  { pattern: /^#\/api-keys$/, handler: renderApiKeys, auth: true, title: 'API keys' },
  { pattern: /^#\/account$/, handler: renderAccount, auth: true, title: 'Account' },
  { pattern: /^#\/admin$/, handler: renderAdmin, admin: true, title: 'Admin' },
  { pattern: /^#\/login$/, handler: renderLogin, title: 'Log in' },
];

// Distinct <title> per page so multiple UNICORN tabs are tellable apart in
// the browser's tab strip / history, instead of every tab reading the same
// generic "UNICORN — play-money prediction markets".
function setPageTitle(pageTitle) {
  document.title = pageTitle ? `${pageTitle} — UNICORN` : 'UNICORN — play-money prediction markets';
}

async function navigate(hash) {
  if (hash && location.hash !== hash) {
    location.hash = hash;
    return; // will re-trigger via hashchange
  }
  clearTrackedIntervals();
  const route = location.hash || '#/markets';
  const match = routes.find(r => r.pattern.test(route));
  renderHeader();
  if (!match) {
    setPageTitle('Page not found');
    appEl.innerHTML = `<div class="empty-state">Page not found.</div>`;
    return;
  }
  if (match.title) setPageTitle(match.title);
  if (match.auth && !state.user) {
    appEl.innerHTML = `<div class="empty-state">Log in to view this page.<br><br><a class="btn primary" href="#/login">Log in / Sign up</a></div>`;
    return;
  }
  if (match.admin && !(state.user && state.user.is_admin)) {
    appEl.innerHTML = `<div class="empty-state">Admin access required.</div>`;
    return;
  }
  const params = match.pattern.exec(route).slice(1);
  try {
    await match.handler(...params);
  } catch (e) {
    appEl.innerHTML = `<div class="card"><p class="error-text">${escapeHtml(e.message)}</p></div>`;
  }
}

window.addEventListener('hashchange', () => navigate());

// ---------- markets list ----------

const MARKET_STATUS_OPTIONS = [
  { value: 'open', label: 'Open' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'all', label: 'All' },
];

async function renderMarketsList() {
  marketsFilterState.search = '';
  marketsFilterState.category = 'All';
  marketsFilterState.status = 'open';
  appEl.innerHTML = `
    <p class="tagline">Chart the odds. Claim the treasure.</p>
    <h1>Markets</h1>
    <p class="muted">Play-money event contracts — trade YES/NO shares that settle at $1 or $0 when the market resolves.</p>
    <div class="ship-divider">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <path d="M12 2v6" stroke="var(--gold)" stroke-width="1.6" stroke-linecap="round"/>
        <circle cx="12" cy="4" r="1.6" fill="var(--gold)"/>
        <path d="M4 10c0 4 3.5 9 8 11 4.5-2 8-7 8-11" stroke="var(--gold)" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M4 10h16" stroke="var(--gold)" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
      <span class="line"></span>
    </div>
    <div class="market-filters">
      <input type="text" id="market-search" placeholder="Search markets…" />
      <div id="status-chips" class="chip-row">
        ${MARKET_STATUS_OPTIONS.map(o => `<button type="button" class="chip${o.value === marketsFilterState.status ? ' selected' : ''}" data-status="${o.value}">${o.label}</button>`).join('')}
      </div>
      <div id="category-chips" class="chip-row"></div>
    </div>
    <div id="markets-list" class="market-list">Loading…</div>`;
  document.getElementById('market-search').oninput = (e) => {
    marketsFilterState.search = e.target.value;
    renderFilteredMarketsList();
  };
  document.querySelectorAll('#status-chips .chip').forEach(btn => {
    btn.onclick = () => {
      if (btn.dataset.status === marketsFilterState.status) return;
      marketsFilterState.status = btn.dataset.status;
      document.querySelectorAll('#status-chips .chip').forEach(c => c.classList.toggle('selected', c === btn));
      loadAndRenderMarketsList(); // status is server-side filtered, unlike category/search — needs a refetch
    };
  });
  await loadAndRenderMarketsList();
  trackInterval(setInterval(loadAndRenderMarketsList, 6000));
  trackInterval(setInterval(tickCountdowns, 1000));
}

async function loadAndRenderMarketsList() {
  const qs = marketsFilterState.status === 'all' ? '' : `?status=${marketsFilterState.status}`;
  const markets = await api(`/api/markets${qs}`, { auth: false });
  lastFetchedMarkets = markets;
  const listEl = document.getElementById('markets-list');
  if (!listEl) return; // navigated away
  renderCategoryChips(markets);
  renderFilteredMarketsList();
}

function renderCategoryChips(markets) {
  const chipsEl = document.getElementById('category-chips');
  if (!chipsEl) return;
  const categories = Array.from(new Set(markets.map(m => m.category))).sort();
  const all = ['All', ...categories];
  // Only rebuild the chip row if the set of categories actually changed —
  // otherwise every 6s refresh would blow away hover/focus state for no reason.
  const existing = Array.from(chipsEl.querySelectorAll('.chip')).map(c => c.dataset.cat);
  if (existing.length === all.length && existing.every((c, i) => c === all[i])) return;
  chipsEl.innerHTML = all.map(cat =>
    `<button type="button" class="chip${cat === marketsFilterState.category ? ' selected' : ''}" data-cat="${escapeHtml(cat)}">${escapeHtml(cat)}</button>`
  ).join('');
  chipsEl.querySelectorAll('.chip').forEach(btn => {
    btn.onclick = () => {
      marketsFilterState.category = btn.dataset.cat;
      chipsEl.querySelectorAll('.chip').forEach(c => c.classList.toggle('selected', c === btn));
      renderFilteredMarketsList();
    };
  });
}

function renderFilteredMarketsList() {
  const listEl = document.getElementById('markets-list');
  if (!listEl) return; // navigated away
  if (lastFetchedMarkets.length === 0) {
    listEl.innerHTML = `<div class="empty-state">No markets yet.${state.user && state.user.is_admin ? ' <a href="#/admin">Create one</a>.' : ''}</div>`;
    return;
  }
  const q = marketsFilterState.search.trim().toLowerCase();
  const cat = marketsFilterState.category;
  const filtered = lastFetchedMarkets.filter(m => {
    if (cat !== 'All' && m.category !== cat) return false;
    if (q && !m.question.toLowerCase().includes(q) && !(m.symbol_label || '').toLowerCase().includes(q)) return false;
    return true;
  });
  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="empty-state">No markets match your search/filter.</div>`;
    return;
  }
  listEl.innerHTML = filtered.map(m => {
    const isTimedFeed = m.is_auto && (m.market_type === 'crypto' || m.market_type === 'commodity' || m.market_type === 'stock' || m.market_type === 'index' || m.market_type === 'forex');
    const isImported = m.is_auto && (m.market_type === 'kalshi' || m.market_type === 'polymarket');
    const statusBit = m.status === 'resolved'
      ? `<span class="tag resolved">Resolved ${m.resolved_outcome}</span>`
      : (isTimedFeed ? `<span class="tag ${countdownUrgencyClass(m.close_time)}" data-countdown="${escapeHtml(m.close_time || '')}">closes in …</span>`
        : (isImported ? `<span class="tag">live</span>` : 'Open'));
    let livePrice = '';
    if (m.status === 'open' && isTimedFeed) {
      livePrice = ` · live ${fmtUnderlyingPrice(m.current_price)} (strike ${fmtUnderlyingPrice(m.strike_price)})`;
    } else if (m.status === 'open' && isImported) {
      livePrice = ` · ${fmtPct(m.current_price)} on ${m.market_type === 'kalshi' ? 'Kalshi' : 'Polymarket'}`;
    }
    return `
    <a class="card market-row" href="#/market/${m.id}">
      <div>
        <div class="market-q">${escapeHtml(m.question)}</div>
        <div class="market-meta">${escapeHtml(m.category)} · ${statusBit}${livePrice}</div>
      </div>
      <div class="price-badge yes">${fmtPct(m.price_yes)}<div class="market-meta" style="text-align:right">YES</div></div>
    </a>
  `;
  }).join('');
  tickCountdowns();
}

function tickCountdowns() {
  document.querySelectorAll('[data-countdown]').forEach(el => {
    const iso = el.getAttribute('data-countdown');
    const text = fmtCountdown(iso);
    el.textContent = text ? `closes in ${text}` : '';
    el.className = `tag ${countdownUrgencyClass(iso)}`;
  });
}

// ---------- market detail ----------

async function renderMarketDetail(idStr) {
  const id = Number(idStr);
  setPageTitle('Market');
  appEl.innerHTML = `<div id="market-detail">Loading…</div>`;
  const m = await api(`/api/markets/${id}`, { auth: false });
  setPageTitle(m.question);

  const isOpen = m.status === 'open';
  const isTimedFeed = m.is_auto && (m.market_type === 'crypto' || m.market_type === 'commodity' || m.market_type === 'stock' || m.market_type === 'index' || m.market_type === 'forex');
  const isImported = m.is_auto && (m.market_type === 'kalshi' || m.market_type === 'polymarket');

  let liveBlock = '';
  if (isTimedFeed) {
    liveBlock = `
    <div class="card" id="live-price-card">
      <h2>${escapeHtml(m.symbol_label || '')} price</h2>
      <p class="muted" style="font-size:20px;font-weight:700;color:var(--text-primary);margin:4px 0;">
        <span id="live-underlying-price">${fmtUnderlyingPrice(m.current_price)}</span>
      </p>
      <p class="muted" style="margin:0;">Strike (opened at): ${fmtUnderlyingPrice(m.strike_price)}${m.settlement_price != null ? ` · Settled at: ${fmtUnderlyingPrice(m.settlement_price)}` : ''}</p>
      ${isOpen ? `<p class="muted" style="margin:8px 0 0;">Closes in <strong id="market-countdown" class="${countdownUrgencyClass(m.close_time)}">${fmtCountdown(m.close_time) || '—'}</strong></p>` : ''}
    </div>`;
  } else if (isImported) {
    const sourceName = m.market_type === 'kalshi' ? 'Kalshi' : 'Polymarket';
    liveBlock = `
    <div class="card" id="live-price-card">
      <h2>Imported from ${sourceName}</h2>
      <p class="muted" style="font-size:20px;font-weight:700;color:var(--text-primary);margin:4px 0;">
        <span id="live-underlying-price">${fmtPct(m.current_price)}</span> <span style="font-size:13px;font-weight:600;color:var(--text-muted);">live on ${sourceName}</span>
      </p>
      <p class="muted" style="margin:0;">Trades here use UNICORN's own play money and don't affect the real ${sourceName} market. Settles automatically once the real market resolves.</p>
      ${m.source_url ? `<p style="margin:8px 0 0;"><a href="${escapeHtml(m.source_url)}" target="_blank" rel="noopener">View original on ${sourceName} ↗</a></p>` : ''}
    </div>`;
  }

  appEl.innerHTML = `
    <div class="market-meta"><a href="#/markets">&larr; All markets</a></div>
    <h1>${escapeHtml(m.question)}</h1>
    <p class="muted">${escapeHtml(m.category)} · ${isOpen ? 'Open' : `Resolved: ${m.resolved_outcome}`}</p>
    ${m.description ? `<p class="muted">${escapeHtml(m.description)}</p>` : ''}
    <div class="detail-grid">
      <div>
        ${liveBlock}
        <div class="card">
          <h2>YES price history</h2>
          <div id="chart-wrap" style="position:relative;"></div>
        </div>
      </div>
      <div>
        <div class="card trade-widget">
          <h2>Trade</h2>
          <div id="trade-area"></div>
        </div>
      </div>
    </div>
  `;

  renderPriceChart(document.getElementById('chart-wrap'), m.price_history, m.price_yes);
  renderTradeWidget(document.getElementById('trade-area'), m);

  if (isOpen) {
    trackInterval(setInterval(() => {
      const el = document.getElementById('market-countdown');
      if (el) {
        el.textContent = fmtCountdown(m.close_time) || '—';
        el.className = countdownUrgencyClass(m.close_time);
      }
    }, 1000));

    trackInterval(setInterval(async () => {
      let fresh;
      try {
        fresh = await api(`/api/markets/${id}`, { auth: false });
      } catch (e) { return; }
      if (!document.getElementById('market-detail') && !document.querySelector('.detail-grid')) return; // navigated away

      if (fresh.status !== m.status) {
        // Market resolved while we were watching — do a full re-render to switch views.
        renderMarketDetail(idStr);
        return;
      }
      m.price_yes = fresh.price_yes;
      m.current_price = fresh.current_price;
      const underlyingEl = document.getElementById('live-underlying-price');
      if (underlyingEl) underlyingEl.textContent = isImported ? fmtPct(fresh.current_price) : fmtUnderlyingPrice(fresh.current_price);
      const chartWrap = document.getElementById('chart-wrap');
      if (chartWrap) renderPriceChart(chartWrap, fresh.price_history, fresh.price_yes);
      const pickYes = document.getElementById('pick-yes');
      const pickNo = document.getElementById('pick-no');
      if (pickYes && pickNo) {
        pickYes.textContent = `YES ${fmtPct(fresh.price_yes)}`;
        pickNo.textContent = `NO ${fmtPct(1 - fresh.price_yes)}`;
      }
    }, 8000));
  }
}

function renderTradeWidget(container, market) {
  if (market.status !== 'open') {
    container.innerHTML = `<p class="muted">This market is resolved. Outcome: <strong>${market.resolved_outcome}</strong>. Winning shares paid $1 each; losing shares paid $0.</p>`;
    return;
  }
  if (!state.user) {
    container.innerHTML = `<p class="muted">Log in to trade.</p><a class="btn primary" href="#/login">Log in / Sign up</a>`;
    return;
  }

  let selected = 'YES';
  container.innerHTML = `
    <div class="outcome-toggle">
      <button id="pick-yes" class="selected yes">YES ${fmtPct(market.price_yes)}</button>
      <button id="pick-no">NO ${fmtPct(1 - market.price_yes)}</button>
    </div>
    <label for="shares-input">Shares to buy</label>
    <input type="number" id="shares-input" min="0.01" step="0.01" value="10" />
    <div class="trade-summary" id="trade-preview">Estimated cost: —</div>
    <div class="error-text" id="trade-error" style="display:none;"></div>
    <div class="success-text" id="trade-success" style="display:none;"></div>
    <button class="primary" id="submit-trade" style="width:100%;margin-top:12px;">Buy shares</button>
    <p class="muted" style="margin-top:14px;">Balance: <span id="live-balance">${fmtMoney(state.user.balance)}</span></p>
  `;

  const pickYes = document.getElementById('pick-yes');
  const pickNo = document.getElementById('pick-no');
  const sharesInput = document.getElementById('shares-input');
  const preview = document.getElementById('trade-preview');
  const errorEl = document.getElementById('trade-error');
  const successEl = document.getElementById('trade-success');

  function selectOutcome(o) {
    selected = o;
    pickYes.classList.toggle('selected', o === 'YES');
    pickNo.classList.toggle('selected', o === 'NO');
    updatePreview();
  }
  function updatePreview() {
    const shares = parseFloat(sharesInput.value) || 0;
    const price = selected === 'YES' ? market.price_yes : (1 - market.price_yes);
    const approxCost = shares * price; // rough estimate; actual AMM cost may differ slightly
    preview.textContent = `Approx. cost: ${fmtMoney(approxCost)} for ${shares} ${selected} shares (pays ${fmtMoney(shares)} if correct, $0 if not)`;
  }
  pickYes.onclick = () => selectOutcome('YES');
  pickNo.onclick = () => selectOutcome('NO');
  sharesInput.oninput = updatePreview;
  updatePreview();

  document.getElementById('submit-trade').onclick = async () => {
    errorEl.style.display = 'none';
    successEl.style.display = 'none';
    const shares = parseFloat(sharesInput.value);
    if (!shares || shares <= 0) {
      errorEl.textContent = 'Enter a positive number of shares.';
      errorEl.style.display = 'block';
      return;
    }
    try {
      const result = await api(`/api/markets/${market.id}/trade`, {
        method: 'POST',
        body: { outcome: selected, shares },
      });
      state.user.balance = result.balance;
      document.getElementById('live-balance').textContent = fmtMoney(result.balance);
      successEl.textContent = `Bought ${shares} ${selected} shares for ${fmtMoney(result.cost)}. New price: ${fmtPct(result.price_yes)} YES.`;
      successEl.style.display = 'block';
      renderHeader();

      // Update in place (don't wipe the success message with a full re-render):
      market.price_yes = result.price_yes;
      pickYes.textContent = `YES ${fmtPct(market.price_yes)}`;
      pickNo.textContent = `NO ${fmtPct(1 - market.price_yes)}`;
      updatePreview();
      const fresh = await api(`/api/markets/${market.id}`, { auth: false });
      const chartWrap = document.getElementById('chart-wrap');
      if (chartWrap) renderPriceChart(chartWrap, fresh.price_history, fresh.price_yes);
    } catch (e) {
      errorEl.textContent = e.message;
      errorEl.style.display = 'block';
    }
  };
}

// ---------- hand-rolled SVG price chart (no external chart library) ----------

function renderPriceChart(container, history, currentPrice) {
  const points = (history && history.length > 0) ? history : [{ t: new Date().toISOString(), price: currentPrice }];
  const w = container.clientWidth > 0 ? container.clientWidth : 560;
  const h = 220;
  const padL = 36, padR = 12, padT = 16, padB = 26;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const xForIndex = (i) => padL + (points.length === 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
  const yForPrice = (p) => padT + (1 - p) * plotH;

  const gridLines = [0, 0.25, 0.5, 0.75, 1].map(p => {
    const y = yForPrice(p);
    return `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="var(--gridline)" stroke-width="1"/>
            <text x="${padL - 8}" y="${y + 4}" font-size="10" fill="var(--text-muted)" text-anchor="end">${Math.round(p * 100)}%</text>`;
  }).join('');

  // A single point (e.g. a freshly-seeded market with no trades yet) has no
  // line segment to stroke — draw it as a flat line across the plot at that
  // price instead of silently rendering nothing.
  const path = points.length === 1
    ? `M ${padL} ${yForPrice(points[0].price).toFixed(1)} L ${(w - padR).toFixed(1)} ${yForPrice(points[0].price).toFixed(1)}`
    : points.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${xForIndex(i).toFixed(1)} ${yForPrice(pt.price).toFixed(1)}`).join(' ');

  const svg = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="YES price history chart">
      ${gridLines}
      <line x1="${padL}" y1="${padT + plotH}" x2="${w - padR}" y2="${padT + plotH}" stroke="var(--baseline)" stroke-width="1"/>
      <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <circle id="chart-hover-dot" cx="0" cy="0" r="4" fill="var(--series-1)" style="display:none;"/>
      <line id="chart-hover-line" x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" stroke="var(--baseline)" stroke-width="1" style="display:none;"/>
      <rect id="chart-hover-target" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent" />
    </svg>
    <div id="chart-tooltip" class="chart-tooltip" style="display:none;"></div>
  `;
  container.innerHTML = svg;

  const svgEl = container.querySelector('svg');
  const dot = document.getElementById('chart-hover-dot');
  const line = document.getElementById('chart-hover-line');
  const tooltip = document.getElementById('chart-tooltip');
  const target = document.getElementById('chart-hover-target');

  target.addEventListener('mousemove', (evt) => {
    const rect = svgEl.getBoundingClientRect();
    const scaleX = w / rect.width;
    const mouseX = (evt.clientX - rect.left) * scaleX;
    let nearest = 0, nearestDist = Infinity;
    points.forEach((pt, i) => {
      const dist = Math.abs(xForIndex(i) - mouseX);
      if (dist < nearestDist) { nearestDist = dist; nearest = i; }
    });
    const pt = points[nearest];
    const x = xForIndex(nearest), y = yForPrice(pt.price);
    dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.style.display = 'block';
    line.setAttribute('x1', x); line.setAttribute('x2', x); line.style.display = 'block';
    const scale = rect.width / w;
    tooltip.style.left = (x * scale) + 'px';
    tooltip.style.top = (y * scale) + 'px';
    tooltip.style.display = 'block';
    tooltip.innerHTML = `<strong>${fmtPct(pt.price)}</strong> YES<br><span style="color:var(--text-muted)">${fmtTime(pt.t)}</span>`;
  });
  target.addEventListener('mouseleave', () => {
    dot.style.display = 'none'; line.style.display = 'none'; tooltip.style.display = 'none';
  });
}

// ---------- portfolio ----------

async function renderPortfolio() {
  appEl.innerHTML = `<h1>Portfolio</h1><div id="portfolio-body">Loading…</div>`;
  const positions = await api('/api/portfolio');
  const body = document.getElementById('portfolio-body');
  body.innerHTML = `<p class="muted">Cash balance: <strong>${fmtMoney(state.user.balance)}</strong></p>`;

  if (positions.length === 0) {
    body.innerHTML += `<div class="empty-state">No open positions yet. <a href="#/markets">Browse markets</a>.</div>`;
    return;
  }
  body.innerHTML += `
    <div class="card">
      <div class="table-scroll">
      <table>
        <thead><tr><th>Market</th><th>Status</th><th class="num">YES shares</th><th class="num">NO shares</th><th class="num">Current YES price</th></tr></thead>
        <tbody>
          ${positions.map(p => `
            <tr>
              <td><a href="#/market/${p.market_id}">${escapeHtml(p.question)}</a></td>
              <td>${p.status === 'resolved' ? `Resolved ${p.resolved_outcome}` : 'Open'}</td>
              <td class="num">${p.shares_yes.toFixed(2)}</td>
              <td class="num">${p.shares_no.toFixed(2)}</td>
              <td class="num">${fmtPct(p.price_yes)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
      </div>
    </div>
  `;
}

// ---------- leaderboard ----------

async function renderLeaderboard() {
  appEl.innerHTML = `
    <h1>Leaderboard</h1>
    <p class="muted">Top traders ranked by net worth — cash balance plus the current value of open positions. Play money only; resets never happen automatically.</p>
    <div id="leaderboard-body">Loading…</div>`;
  const rows = await api('/api/leaderboard', { auth: false });
  const body = document.getElementById('leaderboard-body');
  if (rows.length === 0) {
    body.innerHTML = `<div class="empty-state">No traders yet. <a href="#/login">Sign up</a> to be the first.</div>`;
    return;
  }
  body.innerHTML = `
    <div class="card">
      <div class="table-scroll">
      <table>
        <thead><tr><th>Rank</th><th>Trader</th><th class="num">Net worth</th><th class="num">Cash balance</th></tr></thead>
        <tbody>
          ${rows.map(r => {
            const isMe = state.user && r.username === state.user.username;
            return `
            <tr class="${isMe ? 'me-row' : ''}">
              <td class="${r.rank <= 3 ? 'rank-top rank-' + r.rank : ''}">#${r.rank}</td>
              <td>${escapeHtml(r.username)}${isMe ? ' <span class="tag">you</span>' : ''}</td>
              <td class="num">${fmtMoney(r.net_worth)}</td>
              <td class="num">${fmtMoney(r.balance)}</td>
            </tr>
          `;
          }).join('')}
        </tbody>
      </table>
      </div>
    </div>
  `;
}

// ---------- trade history ----------

function typeLabel(t) {
  const labels = { trade: 'Trade', signup_bonus: 'Signup bonus', payout: 'Payout', deposit: 'Deposit' };
  return labels[t] || t;
}

async function renderHistory() {
  appEl.innerHTML = `<h1>Trade history</h1><div id="history-body">Loading…</div>`;
  const rows = await api('/api/transactions');
  const body = document.getElementById('history-body');
  if (rows.length === 0) {
    body.innerHTML = `<div class="empty-state">No trades yet. <a href="#/markets">Browse markets</a>.</div>`;
    return;
  }
  body.innerHTML = `
    <div class="card">
      <div class="table-scroll">
      <table>
        <thead><tr><th>Date</th><th>Type</th><th>Market</th><th>Outcome</th><th class="num">Shares</th><th class="num">Amount</th><th class="num">Fee</th><th class="num">Balance after</th></tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td>${fmtTime(r.created_at)}</td>
              <td>${typeLabel(r.type)}</td>
              <td>${r.market_id ? `<a href="#/market/${r.market_id}">${escapeHtml(r.market_question || ('#' + r.market_id))}</a>` : '—'}</td>
              <td>${r.outcome || '—'}</td>
              <td class="num">${r.shares ? Number(r.shares).toFixed(2) : '—'}</td>
              <td class="num ${r.amount < 0 ? 'amt-neg' : (r.amount > 0 ? 'amt-pos' : '')}">${fmtMoney(r.amount)}</td>
              <td class="num">${r.fee_amount ? fmtMoney(r.fee_amount) : '—'}</td>
              <td class="num">${fmtMoney(r.balance_after)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
      </div>
    </div>
  `;
}

// ---------- API keys (for bots — see API.md) ----------

function fmtKeyDate(iso) {
  return iso ? fmtTime(iso) : '—';
}

async function renderApiKeys() {
  appEl.innerHTML = `
    <h1>API keys</h1>
    <p class="muted">Generate a key to trade programmatically — a bot uses it exactly like a session token, in an <code>Authorization: Bearer &lt;key&gt;</code> header. Full docs and a Python SDK are in <code>API.md</code>. A key's plaintext is shown exactly once, right after you create it — copy it down immediately, UNICORN only ever stores its hash.</p>
    <div class="card">
      <h2>New key</h2>
      <form id="new-key-form" class="inline-form">
        <input id="new-key-label" type="text" placeholder="Label (e.g. \"momentum bot\")" maxlength="64">
        <label class="checkbox-label"><input id="new-key-can-trade" type="checkbox" checked> Can trade (uncheck for a read-only key)</label>
        <button type="submit" class="btn primary">Generate key</button>
      </form>
      <div id="new-key-result"></div>
    </div>
    <div class="card">
      <h2>Your keys</h2>
      <div id="api-keys-body">Loading…</div>
    </div>
  `;

  document.getElementById('new-key-form').onsubmit = async (e) => {
    e.preventDefault();
    const label = document.getElementById('new-key-label').value.trim();
    const canTrade = document.getElementById('new-key-can-trade').checked;
    const resultEl = document.getElementById('new-key-result');
    try {
      const created = await api('/api/api-keys', { method: 'POST', body: { label, can_trade: canTrade } });
      resultEl.innerHTML = `
        <div class="callout">
          <p><strong>Copy this now — it won't be shown again:</strong></p>
          <code class="key-plaintext">${escapeHtml(created.key)}</code>
        </div>`;
      document.getElementById('new-key-label').value = '';
      await loadApiKeysList();
    } catch (err) {
      resultEl.innerHTML = `<p class="error-text">${escapeHtml(err.message)}</p>`;
    }
  };

  await loadApiKeysList();
}

async function loadApiKeysList() {
  const body = document.getElementById('api-keys-body');
  const keys = await api('/api/api-keys');
  if (keys.length === 0) {
    body.innerHTML = `<div class="empty-state">No API keys yet — generate one above.</div>`;
    return;
  }
  body.innerHTML = `
    <div class="table-scroll">
    <table>
      <thead><tr><th>Label</th><th>Key</th><th>Trading</th><th>Created</th><th>Last used</th><th>Status</th><th></th></tr></thead>
      <tbody>
        ${keys.map(k => `
          <tr>
            <td>${escapeHtml(k.label)}</td>
            <td><code>${escapeHtml(k.key_prefix)}…</code></td>
            <td>${k.can_trade ? 'Trade + read' : 'Read-only'}</td>
            <td>${fmtKeyDate(k.created_at)}</td>
            <td>${fmtKeyDate(k.last_used_at)}</td>
            <td>${k.revoked_at ? `Revoked ${fmtKeyDate(k.revoked_at)}` : 'Active'}</td>
            <td>${k.revoked_at ? '' : `<button class="revoke-key-btn" data-id="${k.id}">Revoke</button>`}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
    </div>
  `;
  body.querySelectorAll('.revoke-key-btn').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('Revoke this API key? Any bot using it will stop working immediately.')) return;
      await api(`/api/api-keys/${btn.dataset.id}`, { method: 'DELETE' });
      await loadApiKeysList();
    };
  });
}

// ---------- account (wallet linking) ----------

async function renderAccount() {
  appEl.innerHTML = `
    <h1>Account</h1>
    <div class="card">
      <h2>Deposit</h2>
      <p class="muted">Play money only — this doesn't touch anything real. It models the fee a real deposit would carry (currently ${DEPOSIT_FEE_LABEL}) so the economics are visible before any of this involves real funds.</p>
      <form id="deposit-form" class="inline-form">
        <input id="deposit-amount" type="number" min="1" max="10000" step="0.01" placeholder="Amount ($)" required>
        <button type="submit" class="btn primary">Deposit</button>
      </form>
      <div id="deposit-result"></div>
    </div>
    <div class="card">
      <h2>Wallet</h2>
      <p class="muted">Link a wallet to sign in with it instead of your password. Linking only proves you control the address — it never moves any real crypto, and your balance stays UNICORN's own play money either way.</p>
      <div id="wallet-status">Loading…</div>
    </div>
  `;
  document.getElementById('deposit-form').onsubmit = async (e) => {
    e.preventDefault();
    const resultEl = document.getElementById('deposit-result');
    const amount = parseFloat(document.getElementById('deposit-amount').value);
    try {
      const result = await api('/api/deposit', { method: 'POST', body: { amount } });
      state.user.balance = result.balance;
      renderHeader();
      resultEl.innerHTML = `
        <div class="callout">
          <p style="margin:0;">Deposited ${fmtMoney(result.gross)} · fee ${fmtMoney(result.fee)} · credited ${fmtMoney(result.net)}. New balance: <strong>${fmtMoney(result.balance)}</strong>.</p>
        </div>`;
      document.getElementById('deposit-amount').value = '';
    } catch (err) {
      resultEl.innerHTML = `<p class="error-text">${escapeHtml(err.message)}</p>`;
    }
  };
  renderWalletStatus();
}

function renderWalletStatus() {
  const el = document.getElementById('wallet-status');
  const addr = state.user && state.user.wallet_address;
  if (addr) {
    el.innerHTML = `
      <p><strong>Linked:</strong> <code>${escapeHtml(addr)}</code></p>
      <button id="unlink-wallet-btn">Unlink wallet</button>
      <div id="wallet-error" class="error-text" style="display:none;"></div>
    `;
    document.getElementById('unlink-wallet-btn').onclick = async () => {
      if (!confirm('Unlink this wallet? You\'ll only be able to log in with your username and password afterward.')) return;
      await api('/api/wallet', { method: 'DELETE' });
      state.user.wallet_address = null;
      renderWalletStatus();
    };
  } else {
    el.innerHTML = `
      <p class="muted">No wallet linked yet.</p>
      <button id="connect-wallet-btn" class="primary">Connect wallet</button>
      <div id="wallet-error" class="error-text" style="display:none;"></div>
    `;
    document.getElementById('connect-wallet-btn').onclick = async () => {
      const errorEl = document.getElementById('wallet-error');
      errorEl.style.display = 'none';
      try {
        const { address, signature } = await connectWalletAndSign();
        const result = await api('/api/wallet/link', { method: 'POST', body: { address, signature } });
        state.user.wallet_address = result.wallet_address;
        renderWalletStatus();
      } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'block';
      }
    };
  }
}

// ---------- admin ----------

async function renderAdmin() {
  appEl.innerHTML = `
    <h1>Admin</h1>
    <div class="card">
      <h2>Deposit fee revenue (play money — models the mechanic, not real revenue)</h2>
      <div id="deposits-summary">Loading…</div>
    </div>
    <div class="card">
      <h2>Live timed markets — a fixed roster of 48 (stocks, crypto &amp; indices)</h2>
      <p class="muted">A curated, definitive board of well-known American names: 12 top US stocks (5-min and 15-min), the 10 most recognizable cryptocurrencies (5-min and 15-min), and the 4 headline US stock indices (15-min) — 48 templates total, all the same "will it be above or below this price" fast win-or-lose format, settling against real live prices. Nothing to do here; the background scheduler manages them. Edit <code>backend/app/scheduler.py</code>'s <code>AUTO_MARKET_CONFIGS</code> to change the roster.</p>
      <h2 style="margin-top:16px;">Kalshi &amp; Polymarket imports — off by default</h2>
      <p class="muted">UNICORN can still pull in trending real-world markets from Kalshi/Polymarket, but that's switched off out of the box (<code>EXTERNAL_IMPORT_MAX_OPEN_TOTAL = 0</code>) to keep the board 100% fast, definitive markets — no slow real-world events to wait hours or days on. Set it above 0 in <code>backend/app/scheduler.py</code> to bring imports back.</p>
    </div>
    <div class="card">
      <h2>Create market</h2>
      <form id="create-market-form">
        <label for="q">Question</label>
        <input type="text" id="q" placeholder="Will X happen by Y date?" required />
        <label for="desc">Description (optional)</label>
        <textarea id="desc" rows="2"></textarea>
        <label for="cat">Category</label>
        <input type="text" id="cat" value="General" />
        <label for="liq">Liquidity parameter (b) — higher = deeper liquidity, less price movement per trade</label>
        <input type="number" id="liq" value="100" min="1" />
        <div class="error-text" id="create-error" style="display:none;"></div>
        <button class="primary" type="submit" style="margin-top:12px;">Create market</button>
      </form>
    </div>
    <div class="card">
      <h2>Resolve an open market</h2>
      <div id="resolve-list">Loading…</div>
    </div>
  `;

  api('/api/admin/deposits-summary').then((s) => {
    document.getElementById('deposits-summary').innerHTML = `
      <p class="muted" style="margin:0 0 10px;">Fee model: ${(s.fee_pct * 100).toFixed(0)}% + ${fmtMoney(s.fee_flat)} per deposit.</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th class="num"># Deposits</th><th class="num">Gross deposited</th><th class="num">Fees collected</th><th class="num">Net credited to users</th></tr></thead>
        <tbody><tr>
          <td class="num">${s.deposit_count}</td>
          <td class="num">${fmtMoney(s.total_gross)}</td>
          <td class="num amt-pos">${fmtMoney(s.total_fees_collected)}</td>
          <td class="num">${fmtMoney(s.total_net_credited)}</td>
        </tr></tbody>
      </table>
      </div>
    `;
  }).catch((e) => {
    document.getElementById('deposits-summary').innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
  });

  document.getElementById('create-market-form').onsubmit = async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById('create-error');
    errorEl.style.display = 'none';
    try {
      await api('/api/markets', {
        method: 'POST',
        body: {
          question: document.getElementById('q').value,
          description: document.getElementById('desc').value,
          category: document.getElementById('cat').value,
          liquidity_b: parseFloat(document.getElementById('liq').value),
        },
      });
      renderAdmin();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = 'block';
    }
  };

  const markets = await api('/api/markets', { auth: false });
  const open = markets.filter(m => m.status === 'open');
  const listEl = document.getElementById('resolve-list');
  if (open.length === 0) {
    listEl.innerHTML = `<p class="muted">No open markets.</p>`;
    return;
  }
  listEl.innerHTML = open.map(m => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--gridline);">
      <div>${escapeHtml(m.question)} <span class="muted">(${fmtPct(m.price_yes)} YES)</span></div>
      <div style="display:flex;gap:8px;">
        <button data-id="${m.id}" data-outcome="YES" class="resolve-btn">Resolve YES</button>
        <button data-id="${m.id}" data-outcome="NO" class="resolve-btn danger">Resolve NO</button>
      </div>
    </div>
  `).join('');

  listEl.querySelectorAll('.resolve-btn').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm(`Resolve market #${btn.dataset.id} as ${btn.dataset.outcome}? This pays out all holders and cannot be undone.`)) return;
      try {
        await api(`/api/markets/${btn.dataset.id}/resolve`, {
          method: 'POST',
          body: { outcome: btn.dataset.outcome },
        });
        renderAdmin();
      } catch (e) {
        alert(e.message);
      }
    };
  });
}

// ---------- login / signup ----------

function renderLogin() {
  appEl.innerHTML = `
    <form class="auth-form card" id="auth-form">
      <h1 id="auth-title">Log in</h1>
      <label for="username">Username</label>
      <input type="text" id="username" required />
      <label for="password">Password</label>
      <input type="password" id="password" required minlength="6" />
      <div class="error-text" id="auth-error" style="display:none;"></div>
      <button class="primary" type="submit" style="width:100%;margin-top:14px;">Log in</button>
      <p style="margin-top:14px;font-size:13px;">
        <span id="auth-switch-text">Don't have an account?</span>
        <button type="button" class="link-btn" id="auth-switch-btn">Sign up</button>
      </p>
      <div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--gridline);" id="wallet-login-block">
        <button type="button" id="wallet-login-btn" style="width:100%;">Log in with wallet</button>
        <p class="muted" style="font-size:12px;margin:6px 0 0;">Only works if you've already linked a wallet from the Account page.</p>
        <div class="error-text" id="wallet-login-error" style="display:none;"></div>
      </div>
    </form>
  `;
  let mode = 'login';
  const title = document.getElementById('auth-title');
  const switchText = document.getElementById('auth-switch-text');
  const switchBtn = document.getElementById('auth-switch-btn');
  const submitBtn = document.querySelector('#auth-form button.primary');
  const errorEl = document.getElementById('auth-error');
  const walletBlock = document.getElementById('wallet-login-block');

  switchBtn.onclick = () => {
    mode = mode === 'login' ? 'signup' : 'login';
    title.textContent = mode === 'login' ? 'Log in' : 'Sign up';
    submitBtn.textContent = mode === 'login' ? 'Log in' : 'Sign up (get 10,000 play dollars)';
    switchText.textContent = mode === 'login' ? "Don't have an account?" : 'Already have an account?';
    switchBtn.textContent = mode === 'login' ? 'Sign up' : 'Log in';
    walletBlock.style.display = mode === 'login' ? 'block' : 'none';
  };

  function applyLoginResult(result) {
    state.token = result.token;
    localStorage.setItem('pm_token', result.token);
    state.user = {
      username: result.username, balance: result.balance,
      is_admin: result.is_admin, wallet_address: result.wallet_address || null,
    };
    navigate('#/markets');
  }

  document.getElementById('auth-form').onsubmit = async (e) => {
    e.preventDefault();
    errorEl.style.display = 'none';
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    try {
      const result = await api(mode === 'login' ? '/api/login' : '/api/signup', {
        method: 'POST', auth: false,
        body: { username, password },
      });
      applyLoginResult(result);
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = 'block';
    }
  };

  document.getElementById('wallet-login-btn').onclick = async () => {
    const walletErrorEl = document.getElementById('wallet-login-error');
    walletErrorEl.style.display = 'none';
    try {
      const { address, signature } = await connectWalletAndSign();
      const result = await api('/api/wallet/login', { method: 'POST', auth: false, body: { address, signature } });
      applyLoginResult(result);
    } catch (err) {
      walletErrorEl.textContent = err.message;
      walletErrorEl.style.display = 'block';
    }
  };
}

// ---------- boot ----------

(async function boot() {
  await refreshMe();
  renderHeader();
  navigate();
})();
