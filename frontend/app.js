// UNICORN demo frontend — vanilla JS, no build step, no external dependencies.

const state = {
  token: localStorage.getItem('pm_token') || null,
  user: null, // { username, balance, is_admin }
  notifications: { unread_count: 0, items: [] },
};
let notifDropdownOpen = false;

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

function fmtTemp(p) {
  if (p === null || p === undefined) return '—';
  return `${Math.round(p)}°F`;
}

// Weather markets' underlying "price" is a Fahrenheit reading, not a
// dollar figure — every other timed-feed market type (crypto/stock/index/
// commodity/forex) still wants fmtUnderlyingPrice. One switch here instead
// of repeating the market_type check at every call site below.
function fmtUnderlying(m, p) {
  return m.market_type === 'weather' ? fmtTemp(p) : fmtUnderlyingPrice(p);
}

// ---------- API helper ----------

// Empty on the web deploy (Railway serves the API from the same origin as
// this file, so plain relative paths like '/api/markets' just work). The
// native mobile wrapper (see mobile/www/index.html) sets
// window.UNICORN_API_BASE to the live Railway URL before this file loads,
// since a bundled-in-the-app-bundle page has no same-origin API to call
// relatively — everything has to be an absolute URL there instead.
const API_BASE = (typeof window !== 'undefined' && window.UNICORN_API_BASE) || '';

async function api(path, { method = 'GET', body = null, auth = true } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  const sentToken = auth && !!state.token;
  if (sentToken) headers['Authorization'] = `Bearer ${state.token}`;
  const res = await fetch(API_BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    // A 401 on a request that actually carried a token means the session
    // itself is gone — expired (see SESSION_IDLE_TIMEOUT_DAYS /
    // SESSION_ABSOLUTE_TIMEOUT_DAYS server-side) or revoked, not "wrong
    // password" (that 401 comes from /api/login with auth:false, so
    // sentToken is false there and this branch doesn't fire). Clear the
    // dead session and send them to log back in instead of leaving
    // whatever form they were using stuck on a bare "Not authenticated".
    if (res.status === 401 && sentToken) {
      logout();
      throw new Error('Your session expired — please log in again.');
    }
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

// The referral "code" is just the referrer's own username (see
// /api/signup's referral_code handling in server.py) — no separate
// code-generation/storage needed, so the link is just the login route
// with a ?ref= query string carrying it.
function referralLink(username) {
  return `${location.origin}${location.pathname}#/login?ref=${encodeURIComponent(username)}`;
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
  state.notifications = { unread_count: 0, items: [] };
  notifDropdownOpen = false;
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
    ['#/faq', 'Rules & FAQ'],
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
    const streak = state.user.daily_streak || 0;
    const claimable = !!state.user.daily_bonus_claimable;
    authAreaEl.innerHTML = `
      <span class="balance-pill">${COIN_SVG}${fmtMoney(state.user.balance)}</span>
      ${streak > 0 ? `<span class="streak-pill" title="Consecutive days you've claimed the daily bonus">${streak}-day streak</span>` : ''}
      ${claimable ? `<button id="daily-bonus-btn" class="primary" type="button">Claim daily bonus</button>` : ''}
      <div class="notif-wrap">
        <button id="notif-bell-btn" type="button" aria-label="Notifications" title="Notifications">
          🔔<span id="notif-badge" class="notif-badge" style="display:none;">0</span>
        </button>
        <div id="notif-dropdown" class="notif-dropdown" style="display:none;"></div>
      </div>
      <span class="muted">${escapeHtml(state.user.username)}</span>
      <button id="logout-btn">Log out</button>
    `;
    document.getElementById('logout-btn').onclick = logout;
    const bonusBtn = document.getElementById('daily-bonus-btn');
    if (bonusBtn) bonusBtn.onclick = claimDailyBonus;
    document.getElementById('notif-bell-btn').onclick = (e) => {
      e.stopPropagation();
      toggleNotifDropdown();
    };
    updateNotifBadge();
    if (notifDropdownOpen) {
      renderNotifDropdown();
      const dropdown = document.getElementById('notif-dropdown');
      if (dropdown) dropdown.style.display = 'block';
      positionNotifDropdown();
    }
  } else {
    authAreaEl.innerHTML = `<a href="#/login" class="btn">Log in / Sign up</a>`;
  }
}

// ---------- notifications bell ----------
//
// Lives outside the router like the activity pulse above it — the bell and
// its unread count are page chrome, not page content, so they persist and
// keep polling across navigation instead of resetting on every route
// change. The dropdown itself is fetched fresh (not just re-rendered from
// cache) every time it's opened, so it never shows stale state from the
// last poll tick.

const NOTIF_POLL_MS = 20000;

function updateNotifBadge() {
  const badge = document.getElementById('notif-badge');
  if (!badge) return;
  const count = state.notifications.unread_count;
  badge.textContent = count > 9 ? '9+' : String(count);
  badge.style.display = count > 0 ? 'inline-flex' : 'none';
}

async function pollNotifications() {
  if (!state.user) return;
  try {
    const data = await api('/api/notifications');
    state.notifications = { unread_count: data.unread_count, items: data.notifications };
    updateNotifBadge();
    if (notifDropdownOpen) renderNotifDropdown();
  } catch (e) {
    // Decorative polling — a failed check just leaves the last known count up.
  }
}

// Positions the (position:fixed) dropdown against the bell's actual
// on-screen location, clamped so it never runs off the left edge on
// narrow screens — plain top/right CSS can't do this since fixed
// positioning is relative to the viewport, not the bell.
function positionNotifDropdown() {
  const bell = document.getElementById('notif-bell-btn');
  const dropdown = document.getElementById('notif-dropdown');
  if (!bell || !dropdown) return;
  const rect = bell.getBoundingClientRect();
  const width = Math.min(320, window.innerWidth - 24);
  let left = rect.right - width;
  left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
  dropdown.style.top = `${rect.bottom + 8}px`;
  dropdown.style.left = `${left}px`;
}

function toggleNotifDropdown() {
  notifDropdownOpen = !notifDropdownOpen;
  const dropdown = document.getElementById('notif-dropdown');
  if (!dropdown) return;
  if (notifDropdownOpen) {
    positionNotifDropdown();
    dropdown.style.display = 'block';
    pollNotifications(); // fetch fresh the moment it's opened, not last poll's cache
  } else {
    dropdown.style.display = 'none';
  }
}

function renderNotifDropdown() {
  const dropdown = document.getElementById('notif-dropdown');
  if (!dropdown) return;
  const items = state.notifications.items;
  const header = `
    <div class="notif-dropdown-header">
      <strong>Notifications</strong>
      ${state.notifications.unread_count > 0 ? `<button type="button" id="notif-mark-all-btn" class="link-btn">mark all read</button>` : ''}
    </div>`;
  const body = items.length === 0
    ? `<p class="muted" style="margin:8px 0;">No notifications yet.</p>`
    : items.map(n => `
      <div class="notif-item${n.is_read ? '' : ' notif-item-unread'}" data-id="${n.id}"${n.market_id ? ` data-market-id="${n.market_id}"` : ''}>
        <div class="notif-item-message">${escapeHtml(n.message)}</div>
        <div class="muted notif-item-time">${fmtTime(n.created_at)}</div>
      </div>`).join('');
  dropdown.innerHTML = header + `<div class="notif-dropdown-body">${body}</div>`;

  const markAllBtn = document.getElementById('notif-mark-all-btn');
  if (markAllBtn) markAllBtn.onclick = async (e) => {
    e.stopPropagation();
    try {
      await api('/api/notifications/read_all', { method: 'POST' });
      await pollNotifications();
    } catch (err) { /* leave as-is on failure */ }
  };
  dropdown.querySelectorAll('.notif-item').forEach(el => {
    el.onclick = async () => {
      const id = el.dataset.id;
      const marketId = el.dataset.marketId;
      if (el.classList.contains('notif-item-unread')) {
        try { await api(`/api/notifications/${id}/read`, { method: 'POST' }); } catch (err) { /* ignore */ }
        await pollNotifications();
      }
      if (marketId) {
        notifDropdownOpen = false;
        navigate(`#/market/${marketId}`);
      }
    };
  });
}

// Clicking anywhere outside the dropdown closes it — a bare document
// listener rather than per-element blur handling, since the dropdown's
// contents (delete/mark-read buttons) already stopPropagation() on the
// clicks that shouldn't bubble up and close it.
document.addEventListener('click', (e) => {
  if (!notifDropdownOpen) return;
  const wrap = document.querySelector('.notif-wrap');
  if (wrap && !wrap.contains(e.target)) {
    notifDropdownOpen = false;
    const dropdown = document.getElementById('notif-dropdown');
    if (dropdown) dropdown.style.display = 'none';
  }
});
window.addEventListener('resize', () => { if (notifDropdownOpen) positionNotifDropdown(); });

// Claims the daily login bonus from the header button — deliberately not a
// full page (it's a one-click, no-form action), so the button itself shows
// the outcome inline rather than routing anywhere. See /api/daily-bonus in
// server.py for the streak/amount math this is just displaying.
async function claimDailyBonus() {
  const btn = document.getElementById('daily-bonus-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Claiming…';
  try {
    const result = await api('/api/daily-bonus', { method: 'POST' });
    state.user.balance = result.balance;
    state.user.daily_streak = result.streak;
    state.user.daily_bonus_claimable = false;
    btn.textContent = `+${fmtMoney(result.amount)} claimed!`;
    // Leave the confirmation up briefly so it's actually readable, then
    // re-render the header to its normal (now non-claimable) state.
    setTimeout(renderHeader, 1600);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Claim daily bonus';
    btn.title = e.message;
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
  // Allows an optional ?ref=<username> query string (a referral link) —
  // renderLogin() reads it straight off location.hash rather than via a
  // capture group here, so this just needs to not reject the route.
  { pattern: /^#\/login(\?.*)?$/, handler: renderLogin, title: 'Log in' },
  { pattern: /^#\/forgot-password$/, handler: renderForgotPassword, title: 'Forgot password' },
  // ?token=... carries the reset token — read off location.hash inside
  // the handler, same pattern as #/login's ?ref= and #/market's own
  // capture group not being reused for query strings.
  { pattern: /^#\/reset-password(\?.*)?$/, handler: renderResetPassword, title: 'Reset password' },
  { pattern: /^#\/faq$/, handler: renderFaq, title: 'Rules & FAQ' },
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
    const isTimedFeed = m.is_auto && (m.market_type === 'crypto' || m.market_type === 'commodity' || m.market_type === 'stock' || m.market_type === 'index' || m.market_type === 'forex' || m.market_type === 'weather');
    const isImported = m.is_auto && (m.market_type === 'kalshi' || m.market_type === 'polymarket');
    const isSportsLive = m.is_auto && (m.market_type === 'sports' || m.market_type === 'odds');
    const statusBit = m.status === 'resolved'
      ? `<span class="tag resolved">Resolved ${m.resolved_outcome}</span>`
      : (isTimedFeed ? `<span class="tag ${countdownUrgencyClass(m.close_time)}" data-countdown="${escapeHtml(m.close_time || '')}">closes in …</span>`
        : ((isImported || isSportsLive) ? `<span class="tag">live</span>` : 'Open'));
    let livePrice = '';
    if (m.status === 'open' && isTimedFeed) {
      livePrice = ` · live ${fmtUnderlying(m, m.current_price)} (strike ${fmtUnderlying(m, m.strike_price)})`;
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
  const isTimedFeed = m.is_auto && (m.market_type === 'crypto' || m.market_type === 'commodity' || m.market_type === 'stock' || m.market_type === 'index' || m.market_type === 'forex' || m.market_type === 'weather');
  const isImported = m.is_auto && (m.market_type === 'kalshi' || m.market_type === 'polymarket');
  const isSportsLive = m.is_auto && (m.market_type === 'sports' || m.market_type === 'odds');
  const underlyingLabel = m.market_type === 'weather' ? 'temperature' : 'price';

  let liveBlock = '';
  if (isTimedFeed) {
    liveBlock = `
    <div class="card" id="live-price-card">
      <h2>${escapeHtml(m.symbol_label || '')} ${underlyingLabel}</h2>
      <p class="muted" style="font-size:20px;font-weight:700;color:var(--text-primary);margin:4px 0;">
        <span id="live-underlying-price">${fmtUnderlying(m, m.current_price)}</span>
      </p>
      <p class="muted" style="margin:0;">Strike (opened at): ${fmtUnderlying(m, m.strike_price)}${m.settlement_price != null ? ` · Settled at: ${fmtUnderlying(m, m.settlement_price)}` : ''}</p>
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
  } else if (isSportsLive) {
    const isOdds = m.market_type === 'odds';
    liveBlock = `
    <div class="card" id="live-price-card">
      <h2>${isOdds ? 'Live moneyline, seeded at real sportsbook odds' : 'Live from MLB'}</h2>
      <p class="muted" style="margin:0;">${isOpen
        ? (isOdds
          ? 'No fixed clock — this resolves the moment the final score is in, following the real game.'
          : 'No fixed clock — this resolves the moment the half-inning is decided, following the real game.')
        : (isOdds ? 'This game has been decided.' : 'This half-inning has been decided.')}</p>
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
        <div class="card" id="comments-card">
          <h2>Discussion</h2>
          <div id="comments-form-area"></div>
          <div id="comments-list">Loading…</div>
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

  renderPriceChart(document.getElementById('chart-wrap'), m.price_history, m.price_yes, { isOpen });
  renderTradeWidget(document.getElementById('trade-area'), m);
  renderCommentForm(id);
  await loadAndRenderComments(id);
  trackInterval(setInterval(() => loadAndRenderComments(id), 15000));

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
      if (underlyingEl) underlyingEl.textContent = isImported ? fmtPct(fresh.current_price) : fmtUnderlying(m, fresh.current_price);
      const chartWrap = document.getElementById('chart-wrap');
      if (chartWrap) renderPriceChart(chartWrap, fresh.price_history, fresh.price_yes, { isOpen: true });
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
      if (chartWrap) renderPriceChart(chartWrap, fresh.price_history, fresh.price_yes, { isOpen: true });
    } catch (e) {
      errorEl.textContent = e.message;
      errorEl.style.display = 'block';
    }
  };
}

// ---------- comments (per-market discussion) ----------

const COMMENT_MAX_LENGTH = 500;

function renderCommentForm(marketId) {
  const el = document.getElementById('comments-form-area');
  if (!el) return;
  if (!state.user) {
    el.innerHTML = `<p class="muted">Log in to join the discussion. <a href="#/login">Log in / Sign up</a></p>`;
    return;
  }
  el.innerHTML = `
    <form id="comment-form">
      <textarea id="comment-body" rows="2" maxlength="${COMMENT_MAX_LENGTH}" placeholder="Say something about this market…"></textarea>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px;">
        <span class="muted" id="comment-count" style="font-size:12px;">0 / ${COMMENT_MAX_LENGTH}</span>
        <button class="primary" type="submit">Post</button>
      </div>
      <div class="error-text" id="comment-error" style="display:none;"></div>
    </form>
  `;
  const bodyInput = document.getElementById('comment-body');
  const countEl = document.getElementById('comment-count');
  bodyInput.oninput = () => { countEl.textContent = `${bodyInput.value.length} / ${COMMENT_MAX_LENGTH}`; };
  document.getElementById('comment-form').onsubmit = async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById('comment-error');
    errorEl.style.display = 'none';
    const body = bodyInput.value.trim();
    if (!body) { errorEl.textContent = 'Comment cannot be empty.'; errorEl.style.display = 'block'; return; }
    try {
      await api(`/api/markets/${marketId}/comments`, { method: 'POST', body: { body } });
      bodyInput.value = '';
      countEl.textContent = `0 / ${COMMENT_MAX_LENGTH}`;
      await loadAndRenderComments(marketId);
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = 'block';
    }
  };
}

async function loadAndRenderComments(marketId) {
  const listEl = document.getElementById('comments-list');
  if (!listEl) return; // navigated away
  let comments;
  try {
    comments = await api(`/api/markets/${marketId}/comments`, { auth: false });
  } catch (e) {
    listEl.innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!document.getElementById('comments-list')) return; // navigated away mid-fetch
  if (comments.length === 0) {
    listEl.innerHTML = `<p class="muted" style="margin:8px 0 0;">No comments yet — be the first.</p>`;
    return;
  }
  listEl.innerHTML = comments.map(c => {
    const canDelete = state.user && (state.user.username === c.username || state.user.is_admin);
    return `
    <div class="comment-item">
      <div class="comment-meta">
        <strong>${escapeHtml(c.username)}</strong>
        <span class="muted">${fmtTime(c.created_at)}</span>
        ${canDelete ? `<button type="button" class="link-btn comment-delete-btn" data-id="${c.id}">delete</button>` : ''}
      </div>
      <div class="comment-body">${escapeHtml(c.body)}</div>
    </div>`;
  }).join('');
  listEl.querySelectorAll('.comment-delete-btn').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm('Delete this comment?')) return;
      try {
        await api(`/api/comments/${btn.dataset.id}`, { method: 'DELETE' });
        await loadAndRenderComments(marketId);
      } catch (e) {
        alert(e.message);
      }
    };
  });
}

// ---------- hand-rolled SVG price chart (no external chart library) ----------

function renderPriceChart(container, history, currentPrice, opts = {}) {
  const isOpen = !!opts.isOpen;
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
  const lastX = points.length === 1 ? (w - padR) : xForIndex(points.length - 1);
  const path = points.length === 1
    ? `M ${padL} ${yForPrice(points[0].price).toFixed(1)} L ${lastX.toFixed(1)} ${yForPrice(points[0].price).toFixed(1)}`
    : points.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${xForIndex(i).toFixed(1)} ${yForPrice(pt.price).toFixed(1)}`).join(' ');

  // Same path, closed down to the baseline and back — a soft top-to-bottom
  // fade under the line instead of a flat block, so it reads as a glow.
  const baselineY = padT + plotH;
  const areaPath = `${path} L ${lastX.toFixed(1)} ${baselineY} L ${padL} ${baselineY} Z`;

  const prices = points.map(pt => pt.price);
  const rangeCaption = points.length > 1
    ? `<div class="chart-range muted">Range ${fmtPct(Math.min(...prices))}–${fmtPct(Math.max(...prices))} · ${points.length} price point${points.length === 1 ? '' : 's'}</div>`
    : '';

  // A gently pulsing dot on the most recent point for an open market — a
  // quick visual "this is still live", distinct from the static hover dot
  // (which only appears on mouseover, see below).
  const lastY = yForPrice(points[points.length - 1].price);
  const liveDot = isOpen
    ? `<circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="8" fill="var(--series-1)" opacity="0.25" class="chart-live-pulse"/>
       <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5" fill="var(--series-1)" stroke="var(--page)" stroke-width="1.5"/>`
    : '';

  const svg = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="YES price history chart">
      <defs>
        <linearGradient id="chart-area-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" style="stop-color:var(--series-1);stop-opacity:0.28"/>
          <stop offset="100%" style="stop-color:var(--series-1);stop-opacity:0"/>
        </linearGradient>
      </defs>
      ${gridLines}
      <line x1="${padL}" y1="${padT + plotH}" x2="${w - padR}" y2="${padT + plotH}" stroke="var(--baseline)" stroke-width="1"/>
      <path d="${areaPath}" fill="url(#chart-area-fill)" stroke="none"/>
      <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      ${liveDot}
      <circle id="chart-hover-dot" cx="0" cy="0" r="4" fill="var(--series-1)" style="display:none;"/>
      <line id="chart-hover-line" x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" stroke="var(--baseline)" stroke-width="1" style="display:none;"/>
      <rect id="chart-hover-target" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent" />
    </svg>
    ${rangeCaption}
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

// Second hand-rolled SVG chart, separate from renderPriceChart() above:
// that one assumes a fixed 0-100% probability axis (market YES price),
// this one plots dollar balances with a dynamic min/max range — different
// enough in scale handling that sharing one function would mean branching
// on axis type throughout, rather than just having two small focused ones.
function renderBalanceChart(container, points) {
  if (!points || points.length === 0) {
    container.innerHTML = `<p class="muted" style="font-size:13px;">Not enough history yet for a chart.</p>`;
    return;
  }
  const w = container.clientWidth > 0 ? container.clientWidth : 560;
  const h = 180;
  // padL is wider than renderPriceChart's (36px, for short "100%" labels) --
  // dollar-formatted labels like "$10,005.78" run noticeably longer and were
  // clipping their leading "$" against the SVG's own edge (SVG's default
  // overflow:hidden crops anything with a negative x) at the old 60px.
  const padL = 74, padR = 12, padT = 16, padB = 26;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const values = points.map(p => p.balance);
  let minV = Math.min(...values), maxV = Math.max(...values);
  if (minV === maxV) { minV -= 1; maxV += 1; } // flat history -- avoid a zero-height range
  const rangePad = (maxV - minV) * 0.08;
  minV -= rangePad; maxV += rangePad;

  const xForIndex = (i) => padL + (points.length === 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
  const yForValue = (v) => padT + (1 - (v - minV) / (maxV - minV)) * plotH;

  const gridSteps = 4;
  const gridLines = Array.from({ length: gridSteps + 1 }, (_, i) => {
    const v = minV + (maxV - minV) * (i / gridSteps);
    const y = yForValue(v);
    return `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="var(--gridline)" stroke-width="1"/>
            <text x="${padL - 8}" y="${y + 4}" font-size="10" fill="var(--text-muted)" text-anchor="end">${fmtMoney(v)}</text>`;
  }).join('');

  const lastX = points.length === 1 ? (w - padR) : xForIndex(points.length - 1);
  const path = points.length === 1
    ? `M ${padL} ${yForValue(points[0].balance).toFixed(1)} L ${lastX.toFixed(1)} ${yForValue(points[0].balance).toFixed(1)}`
    : points.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${xForIndex(i).toFixed(1)} ${yForValue(pt.balance).toFixed(1)}`).join(' ');
  const baselineY = padT + plotH;
  const areaPath = `${path} L ${lastX.toFixed(1)} ${baselineY} L ${padL} ${baselineY} Z`;

  container.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Balance history chart">
      <defs>
        <linearGradient id="balance-chart-area-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" style="stop-color:var(--series-1);stop-opacity:0.28"/>
          <stop offset="100%" style="stop-color:var(--series-1);stop-opacity:0"/>
        </linearGradient>
      </defs>
      ${gridLines}
      <path d="${areaPath}" fill="url(#balance-chart-area-fill)" stroke="none"/>
      <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <circle id="balance-chart-hover-dot" cx="0" cy="0" r="4" fill="var(--series-1)" style="display:none;"/>
      <line id="balance-chart-hover-line" x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" stroke="var(--baseline)" stroke-width="1" style="display:none;"/>
      <rect id="balance-chart-hover-target" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent" />
    </svg>
    <div id="balance-chart-tooltip" class="chart-tooltip" style="display:none;"></div>
  `;

  const svgEl = container.querySelector('svg');
  const dot = document.getElementById('balance-chart-hover-dot');
  const line = document.getElementById('balance-chart-hover-line');
  const tooltip = document.getElementById('balance-chart-tooltip');
  const target = document.getElementById('balance-chart-hover-target');

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
    const x = xForIndex(nearest), y = yForValue(pt.balance);
    dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.style.display = 'block';
    line.setAttribute('x1', x); line.setAttribute('x2', x); line.style.display = 'block';
    const scale = rect.width / w;
    tooltip.style.left = (x * scale) + 'px';
    tooltip.style.top = (y * scale) + 'px';
    tooltip.style.display = 'block';
    tooltip.innerHTML = `<strong>${fmtMoney(pt.balance)}</strong><br><span style="color:var(--text-muted)">${fmtTime(pt.t)}</span>`;
  });
  target.addEventListener('mouseleave', () => {
    dot.style.display = 'none'; line.style.display = 'none'; tooltip.style.display = 'none';
  });
}

function pnlTile(label, entry, emptyLabel) {
  if (!entry) {
    return `<div class="stat-tile"><div class="stat-label">${label}</div><div class="stat-value">—</div><div class="stat-sub">${emptyLabel}</div></div>`;
  }
  const cls = entry.pnl >= 0 ? 'amt-pos' : 'amt-neg';
  const sign = entry.pnl >= 0 ? '+' : '';
  return `
    <div class="stat-tile">
      <div class="stat-label">${label}</div>
      <div class="stat-value ${cls}">${sign}${fmtMoney(entry.pnl)}</div>
      <div class="stat-sub" title="${escapeHtml(entry.question)}">${escapeHtml(entry.question)}</div>
    </div>`;
}

async function renderPortfolio() {
  appEl.innerHTML = `<h1>Portfolio</h1><div id="portfolio-body">Loading…</div>`;
  const [positions, stats] = await Promise.all([api('/api/portfolio'), api('/api/portfolio/stats')]);
  const body = document.getElementById('portfolio-body');
  body.innerHTML = `<p class="muted">Cash balance: <strong>${fmtMoney(state.user.balance)}</strong></p>`;

  // Performance card — only worth showing once at least one market this
  // account traded has actually resolved; otherwise every stat here would
  // just be a wall of "—" placeholders, which isn't useful.
  if (stats.resolved_markets_traded > 0) {
    const pnlCls = stats.total_realized_pnl >= 0 ? 'amt-pos' : 'amt-neg';
    const pnlSign = stats.total_realized_pnl >= 0 ? '+' : '';
    body.innerHTML += `
      <div class="card">
        <h2>Performance</h2>
        <div class="stats-grid">
          <div class="stat-tile">
            <div class="stat-label">Win rate</div>
            <div class="stat-value">${stats.win_rate !== null ? fmtPct(stats.win_rate) : '—'}</div>
            <div class="stat-sub">${stats.wins}W&nbsp;/&nbsp;${stats.losses}L</div>
          </div>
          <div class="stat-tile">
            <div class="stat-label">Realized P&amp;L</div>
            <div class="stat-value ${pnlCls}">${pnlSign}${fmtMoney(stats.total_realized_pnl)}</div>
            <div class="stat-sub">${stats.resolved_markets_traded} resolved market${stats.resolved_markets_traded === 1 ? '' : 's'}</div>
          </div>
          ${pnlTile('Biggest win', stats.biggest_win, 'No wins yet')}
          ${pnlTile('Biggest loss', stats.biggest_loss, 'No losses yet')}
        </div>
        <div id="balance-chart-container" style="margin-top:16px;"></div>
      </div>
    `;
    renderBalanceChart(document.getElementById('balance-chart-container'), stats.balance_history);
  }

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

const LEADERBOARD_BOARD_OPTIONS = [
  { value: 'all', label: 'All traders' },
  { value: 'humans', label: 'Humans' },
  { value: 'bots', label: 'Bots' },
];
let leaderboardBoard = 'all';

async function renderLeaderboard() {
  leaderboardBoard = 'all';
  appEl.innerHTML = `
    <h1>Leaderboard</h1>
    <p class="muted">Top traders ranked by net worth — cash balance plus the current value of open positions. Play money only; resets never happen automatically. "Bots" is any account that's ever placed a trade using an <a href="#/faq">API key</a> instead of logging in by hand.</p>
    <div class="chip-row" id="board-chips" style="margin-bottom:14px;">
      ${LEADERBOARD_BOARD_OPTIONS.map(o => `<button type="button" class="chip${o.value === leaderboardBoard ? ' selected' : ''}" data-board="${o.value}">${o.label}</button>`).join('')}
    </div>
    <div id="leaderboard-body">Loading…</div>`;
  document.querySelectorAll('#board-chips .chip').forEach(btn => {
    btn.onclick = () => {
      if (btn.dataset.board === leaderboardBoard) return;
      leaderboardBoard = btn.dataset.board;
      document.querySelectorAll('#board-chips .chip').forEach(c => c.classList.toggle('selected', c === btn));
      loadAndRenderLeaderboard();
    };
  });
  await loadAndRenderLeaderboard();
}

async function loadAndRenderLeaderboard() {
  const body = document.getElementById('leaderboard-body');
  if (!body) return; // navigated away
  body.innerHTML = 'Loading…';
  const qs = leaderboardBoard === 'all' ? '' : `?board=${leaderboardBoard}`;
  const rows = await api(`/api/leaderboard${qs}`, { auth: false });
  if (!document.getElementById('leaderboard-body')) return; // navigated away mid-fetch
  if (rows.length === 0) {
    body.innerHTML = leaderboardBoard === 'bots'
      ? `<div class="empty-state">No bot-driven accounts yet — trade via an <a href="#/api-keys">API key</a> to be the first.</div>`
      : `<div class="empty-state">No traders yet. <a href="#/login">Sign up</a> to be the first.</div>`;
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
  const labels = { trade: 'Trade', signup_bonus: 'Signup bonus', payout: 'Payout', deposit: 'Deposit', daily_bonus: 'Daily bonus', referral_bonus: 'Referral bonus', weekly_challenge: 'Weekly challenge' };
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

function renderBadgeGrid(container, achievementsList) {
  container.innerHTML = achievementsList.map(a => `
    <div class="badge-tile ${a.earned ? 'earned' : 'locked'}" title="${a.earned && a.earned_at ? 'Earned ' + fmtTime(a.earned_at) : 'Not earned yet'}">
      <div class="badge-icon">${a.earned ? '★' : '☆'}</div>
      <div class="badge-label">${escapeHtml(a.label)}</div>
      <div class="badge-desc">${escapeHtml(a.description)}</div>
    </div>
  `).join('');
}

// Same tile layout as achievements, but these reset weekly (see
// /api/challenges) rather than being earned once forever — 'completed'
// only ever means "already claimed this week", not "permanently done".
function renderChallengeGrid(container, challengeList) {
  container.innerHTML = challengeList.map(c => `
    <div class="badge-tile challenge-tile ${c.completed ? 'earned' : 'locked'}" title="${c.completed && c.completed_at ? 'Completed ' + fmtTime(c.completed_at) : 'Not completed yet this week'}">
      <div class="badge-icon">${c.completed ? '✓' : '○'}</div>
      <div class="badge-label">${escapeHtml(c.label)} <span class="challenge-reward">+${fmtMoney(c.reward)}</span></div>
      <div class="badge-desc">${escapeHtml(c.description)}</div>
    </div>
  `).join('');
}

// Real-money mode is off on every deploy until UNICORN_REAL_MONEY_ENABLED
// is set (see backend/app/realmoney.py) — state.user.real_money_enabled
// comes straight from /api/me, so this whole card just doesn't render at
// all on a demo deploy. Three branches: not yet verified (or a past
// rejection) shows the KYC form; pending shows a holding message; verified
// unlocks the real balance + deposit/withdraw forms.
function realMoneySectionHtml(status) {
  if (status === 'verified') {
    return `
      <p class="muted">Real balance: <strong>${fmtMoney(state.user.real_balance || 0)}</strong></p>
      <form id="real-deposit-form" class="inline-form">
        <input id="real-deposit-amount" type="number" min="1" max="10000" step="0.01" placeholder="Amount ($)" required>
        <button type="submit" class="btn primary">Deposit</button>
      </form>
      <div id="real-deposit-result"></div>
      <form id="real-withdraw-form" class="inline-form" style="margin-top:10px;">
        <input id="real-withdraw-amount" type="number" min="1" step="0.01" placeholder="Amount ($)" required>
        <button type="submit" class="btn">Withdraw</button>
      </form>
      <div id="real-withdraw-result"></div>
      <div id="real-money-history" class="muted" style="font-size:13px;margin-top:10px;">Loading transaction history…</div>
    `;
  }
  if (status === 'pending') {
    return `<p class="muted">Your identity verification is under review — check back soon.</p>`;
  }
  return `
    ${status === 'rejected' ? `<p class="error-text" id="kyc-rejection-reason">Your last submission wasn't approved.</p>` : ''}
    <p class="muted">Verify your identity to unlock real-money deposits and withdrawals once this deploy's licensing is in place.</p>
    <form id="kyc-form">
      <label for="kyc-legal-name">Legal name</label>
      <input type="text" id="kyc-legal-name" required />
      <label for="kyc-dob">Date of birth</label>
      <input type="date" id="kyc-dob" required />
      <label for="kyc-address">Address</label>
      <input type="text" id="kyc-address" required />
      <label for="kyc-state">State (2-letter)</label>
      <input type="text" id="kyc-state" maxlength="2" style="text-transform:uppercase;" required />
      <div class="error-text" id="kyc-error" style="display:none;"></div>
      <button class="primary" type="submit" style="margin-top:12px;">Submit for review</button>
    </form>
  `;
}

async function renderAccount() {
  appEl.innerHTML = `
    <h1>Account</h1>
    <div class="card">
      <h2>Achievements</h2>
      <div class="badge-grid" id="badge-grid">Loading…</div>
    </div>
    <div class="card">
      <h2>Weekly challenges</h2>
      <p class="muted" id="challenges-subtitle">Resets weekly — complete any of these before it resets to claim the reward.</p>
      <div class="badge-grid" id="challenge-grid">Loading…</div>
    </div>
    <div class="card">
      <h2>Invite friends</h2>
      <p class="muted">Share your link — when someone signs up through it, you both get <strong>${fmtMoney(250)}</strong> in play money.</p>
      <div class="inline-form">
        <input id="referral-link" type="text" readonly value="${escapeHtml(referralLink(state.user.username))}" />
        <button type="button" id="copy-referral-btn" class="btn">Copy link</button>
      </div>
      <div id="referral-stats" class="muted" style="font-size:13px;margin-top:10px;">Loading…</div>
    </div>
    <div class="card">
      <h2>Deposit</h2>
      <p class="muted">Play money only — this doesn't touch anything real. It models the fee a real deposit would carry (currently ${DEPOSIT_FEE_LABEL}) so the economics are visible before any of this involves real funds.</p>
      <form id="deposit-form" class="inline-form">
        <input id="deposit-amount" type="number" min="1" max="10000" step="0.01" placeholder="Amount ($)" required>
        <button type="submit" class="btn primary">Deposit</button>
      </form>
      <div id="deposit-result"></div>
    </div>
    ${state.user.real_money_enabled ? `
    <div class="card">
      <h2>Real-money mode</h2>
      ${realMoneySectionHtml(state.user.kyc_status || 'unverified')}
    </div>
    ` : ''}
    <div class="card">
      <h2>Wallet</h2>
      <p class="muted">Link a wallet to sign in with it instead of your password. Linking only proves you control the address — it never moves any real crypto, and your balance stays UNICORN's own play money either way.</p>
      <div id="wallet-status">Loading…</div>
    </div>
    <div class="card">
      <h2>Email</h2>
      <p class="muted">Optional — add one so "Forgot password?" on the login page has somewhere to send a reset link. Signing up never required one, so this stays blank until you set it.</p>
      <form id="email-form" class="inline-form">
        <input id="email-input" type="email" placeholder="you@example.com" value="${escapeHtml(state.user.email || '')}" required />
        <button type="submit" class="btn primary">${state.user.email ? 'Update' : 'Save'}</button>
      </form>
      <div id="email-result"></div>
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
  if (state.user.real_money_enabled) {
    const kycStatus = state.user.kyc_status || 'unverified';
    if (kycStatus === 'verified') {
      document.getElementById('real-deposit-form').onsubmit = async (e) => {
        e.preventDefault();
        const resultEl = document.getElementById('real-deposit-result');
        const amount = parseFloat(document.getElementById('real-deposit-amount').value);
        try {
          const result = await api('/api/real-money/deposit', { method: 'POST', body: { amount } });
          state.user.real_balance = result.real_balance;
          resultEl.innerHTML = result.status === 'completed'
            ? `<p class="muted" style="font-size:13px;">Deposited. New real balance: <strong>${fmtMoney(result.real_balance)}</strong>.</p>`
            : `<p class="muted" style="font-size:13px;">Deposit ${escapeHtml(result.status)}.</p>`;
          document.getElementById('real-deposit-amount').value = '';
        } catch (err) {
          resultEl.innerHTML = `<p class="error-text">${escapeHtml(err.message)}</p>`;
        }
      };
      document.getElementById('real-withdraw-form').onsubmit = async (e) => {
        e.preventDefault();
        const resultEl = document.getElementById('real-withdraw-result');
        const amount = parseFloat(document.getElementById('real-withdraw-amount').value);
        try {
          const result = await api('/api/real-money/withdraw', { method: 'POST', body: { amount } });
          state.user.real_balance = result.real_balance;
          resultEl.innerHTML = result.status === 'completed'
            ? `<p class="muted" style="font-size:13px;">Withdrawal sent. New real balance: <strong>${fmtMoney(result.real_balance)}</strong>.</p>`
            : `<p class="muted" style="font-size:13px;">Withdrawal ${escapeHtml(result.status)}.</p>`;
          document.getElementById('real-withdraw-amount').value = '';
        } catch (err) {
          resultEl.innerHTML = `<p class="error-text">${escapeHtml(err.message)}</p>`;
        }
      };
      api('/api/real-money/transactions').then(r => {
        const el = document.getElementById('real-money-history');
        if (!el) return; // guard: user may have navigated away before this resolves
        if (!r.transactions.length) {
          el.textContent = 'No real-money transactions yet.';
          return;
        }
        el.innerHTML = r.transactions.map(t =>
          `${escapeHtml(t.type)} · ${fmtMoney(t.amount)} · ${escapeHtml(t.status)} · ${escapeHtml(t.created_at)}`
        ).join('<br>');
      }).catch(() => {
        const el = document.getElementById('real-money-history');
        if (el) el.textContent = "Couldn't load transaction history right now.";
      });
    } else {
      const kycForm = document.getElementById('kyc-form');
      if (kycForm) {
        kycForm.onsubmit = async (e) => {
          e.preventDefault();
          const errorEl = document.getElementById('kyc-error');
          errorEl.style.display = 'none';
          try {
            const result = await api('/api/kyc/submit', {
              method: 'POST',
              body: {
                legal_name: document.getElementById('kyc-legal-name').value.trim(),
                date_of_birth: document.getElementById('kyc-dob').value,
                address: document.getElementById('kyc-address').value.trim(),
                state: document.getElementById('kyc-state').value.trim().toUpperCase(),
              },
            });
            state.user.kyc_status = result.status;
            renderAccount();
          } catch (err) {
            errorEl.textContent = err.message;
            errorEl.style.display = 'block';
          }
        };
      }
      if (kycStatus === 'rejected') {
        api('/api/kyc/status').then(r => {
          const el = document.getElementById('kyc-rejection-reason');
          if (el && r.latest_submission && r.latest_submission.rejection_reason) {
            el.textContent = `Your last submission wasn't approved: ${r.latest_submission.rejection_reason}`;
          }
        }).catch(() => {});
      }
    }
  }
  document.getElementById('email-form').onsubmit = async (e) => {
    e.preventDefault();
    const resultEl = document.getElementById('email-result');
    const email = document.getElementById('email-input').value.trim();
    try {
      const result = await api('/api/account/email', { method: 'POST', body: { email } });
      state.user.email = result.email;
      resultEl.innerHTML = `<p class="muted" style="font-size:13px;">Saved.</p>`;
    } catch (err) {
      resultEl.innerHTML = `<p class="error-text">${escapeHtml(err.message)}</p>`;
    }
  };
  api('/api/achievements').then(list => {
    const grid = document.getElementById('badge-grid');
    if (grid) renderBadgeGrid(grid, list); // guard: user may have navigated away before this resolves
  }).catch(() => {
    const grid = document.getElementById('badge-grid');
    if (grid) grid.innerHTML = `<p class="muted" style="font-size:13px;">Couldn't load achievements right now.</p>`;
  });
  api('/api/challenges').then(result => {
    const grid = document.getElementById('challenge-grid');
    if (grid) renderChallengeGrid(grid, result.challenges);
    const subtitle = document.getElementById('challenges-subtitle');
    if (subtitle) subtitle.textContent = `Resets ${fmtTime(result.resets_at)} — complete any of these before then to claim the reward.`;
    // Fetching this endpoint can itself just have credited a
    // newly-completed challenge's reward (see sync_challenges() in
    // server.py) — reflect that in the header immediately rather than
    // only after the next full page load.
    if (state.user && result.balance !== state.user.balance) {
      state.user.balance = result.balance;
      renderHeader();
    }
  }).catch(() => {
    const grid = document.getElementById('challenge-grid');
    if (grid) grid.innerHTML = `<p class="muted" style="font-size:13px;">Couldn't load challenges right now.</p>`;
  });
  document.getElementById('copy-referral-btn').onclick = async (e) => {
    const input = document.getElementById('referral-link');
    input.select();
    const btn = e.currentTarget;
    try {
      await navigator.clipboard.writeText(input.value);
    } catch (err) {
      // Clipboard API can be unavailable (older browser, non-HTTPS, denied
      // permission) — the input is already selected as a manual fallback,
      // so this isn't a dead end, just a less one-click one.
    }
    const original = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = original; }, 1600);
  };
  api('/api/referrals').then(r => {
    const el = document.getElementById('referral-stats');
    if (!el) return; // guard: user may have navigated away before this resolves
    if (r.referral_count === 0) {
      el.textContent = 'No referrals yet — share your link above.';
    } else {
      el.innerHTML = `<strong>${r.referral_count}</strong> friend${r.referral_count === 1 ? '' : 's'} referred · <strong>${fmtMoney(r.total_bonus_earned)}</strong> earned from referrals`;
    }
  }).catch(() => {
    const el = document.getElementById('referral-stats');
    if (el) el.innerHTML = `<p class="muted" style="font-size:13px;margin:0;">Couldn't load referral stats right now.</p>`;
  });
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

async function loadKycQueue() {
  const el = document.getElementById('kyc-queue');
  if (!el) return; // guard: admin may have navigated away before this resolves
  try {
    const { submissions } = await api('/api/admin/kyc?status=pending');
    if (!submissions.length) {
      el.innerHTML = `<p class="muted">No pending KYC submissions.</p>`;
      return;
    }
    el.innerHTML = submissions.map(s => `
      <div style="padding:10px 0;border-bottom:1px solid var(--gridline);">
        <div><strong>${escapeHtml(s.username)}</strong> — ${escapeHtml(s.legal_name)} · ${escapeHtml(s.state)} · DOB ${escapeHtml(s.date_of_birth)}</div>
        <div class="muted" style="font-size:13px;">${escapeHtml(s.address)}</div>
        <div style="display:flex;gap:8px;margin-top:8px;">
          <button data-id="${s.id}" class="kyc-approve-btn">Approve</button>
          <button data-id="${s.id}" class="kyc-reject-btn danger">Reject</button>
        </div>
      </div>
    `).join('');
    el.querySelectorAll('.kyc-approve-btn').forEach(btn => {
      btn.onclick = async () => {
        if (!confirm('Approve this KYC submission? This unlocks real-money deposits/withdrawals for this account.')) return;
        try {
          await api(`/api/admin/kyc/${btn.dataset.id}/approve`, { method: 'POST' });
          loadKycQueue();
        } catch (e) {
          alert(e.message);
        }
      };
    });
    el.querySelectorAll('.kyc-reject-btn').forEach(btn => {
      btn.onclick = async () => {
        const reason = prompt('Reason for rejecting this submission?') || '';
        try {
          await api(`/api/admin/kyc/${btn.dataset.id}/reject`, { method: 'POST', body: { reason } });
          loadKycQueue();
        } catch (e) {
          alert(e.message);
        }
      };
    });
  } catch (e) {
    el.innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
  }
}

async function renderAdmin() {
  appEl.innerHTML = `
    <h1>Admin</h1>
    ${state.user.real_money_enabled ? `
    <div class="card">
      <h2>Real-money KYC queue</h2>
      <div id="kyc-queue">Loading…</div>
    </div>
    ` : ''}
    <div class="card">
      <h2>Deposit fee revenue (play money — models the mechanic, not real revenue)</h2>
      <div id="deposits-summary">Loading…</div>
    </div>
    <div class="card">
      <h2>Live timed markets — a fixed roster of 59 (stocks, crypto, indices, commodities, forex &amp; weather)</h2>
      <p class="muted">A curated, definitive board of well-known American names: 12 top US stocks (5-min and 15-min), the 10 most recognizable cryptocurrencies (5-min and 15-min), the 4 headline US stock indices (15-min), 3 major commodities (gold/silver/crude, 15-min), 3 major currency pairs (15-min), and 5 major-metro temperature readings (15-min) — 59 templates total, all the same "will it be above or below this reading" fast win-or-lose format, settling against real live prices/temperatures. Nothing to do here; the background scheduler manages them. Edit <code>backend/app/scheduler.py</code>'s <code>AUTO_MARKET_CONFIGS</code> to change the roster.</p>
      <h2 style="margin-top:16px;">Sports — live MLB half-innings, no fixed clock</h2>
      <p class="muted">Separate from the fixed roster above: while an MLB game is live, UNICORN opens a market for the current half-inning ("Will the Yankees score in the bottom of the 9th?") sourced from MLB's public Stats API, and resolves it the moment a run scores or the half-inning ends scoreless — no fixed 5/15-min clock, it just follows the real game. See <code>sports_tick()</code> in <code>backend/app/scheduler.py</code> and <code>backend/app/sports_feed.py</code>.</p>
      <h2 style="margin-top:16px;">Moneylines — NFL/NBA/MLB/NHL, seeded at real sportsbook odds</h2>
      <p class="muted">Also separate from the fixed roster: for upcoming/live games in the four major US leagues, UNICORN opens a "will the home team win" market seeded at the real, de-vigged implied probability from live sportsbook odds (via The Odds API), and resolves it once the final score is in. Needs an <code>ODDS_API_KEY</code> environment variable — silently does nothing without one, same as the Kalshi/Polymarket imports below being off by default. This is the one feed here backed by a metered API, so it syncs roughly hourly rather than every tick; see <code>odds_tick()</code> in <code>backend/app/scheduler.py</code> and <code>backend/app/odds_feed.py</code>.</p>
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

  if (state.user.real_money_enabled) {
    loadKycQueue();
  }

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
  // A referral link looks like #/login?ref=someusername — pull the code
  // straight off location.hash (the route regex just allows the query
  // string through, it doesn't capture it) rather than parsing
  // location.search, since everything after "#" is opaque to the browser's
  // own URL parsing.
  const hashQuery = location.hash.split('?')[1] || '';
  const referralCode = new URLSearchParams(hashQuery).get('ref') || '';
  appEl.innerHTML = `
    <form class="auth-form card" id="auth-form">
      <h1 id="auth-title">${referralCode ? 'Sign up' : 'Log in'}</h1>
      ${referralCode ? `<p class="muted" style="margin-top:-8px;font-size:13px;">Invited by <strong>${escapeHtml(referralCode)}</strong> — sign up and you'll both get a $250 play-money bonus.</p>` : ''}
      <label for="username">Username</label>
      <input type="text" id="username" required />
      <label for="password">Password</label>
      <input type="password" id="password" required minlength="6" />
      <p id="forgot-password-row" style="margin:6px 0 0;font-size:13px;${referralCode ? 'display:none;' : ''}">
        <button type="button" class="link-btn" id="forgot-password-btn">Forgot password?</button>
      </p>
      <div class="error-text" id="auth-error" style="display:none;"></div>
      <button class="primary" type="submit" style="width:100%;margin-top:14px;">${referralCode ? 'Sign up (get 10,250 play dollars)' : 'Log in'}</button>
      <p style="margin-top:14px;font-size:13px;">
        <span id="auth-switch-text">${referralCode ? 'Already have an account?' : "Don't have an account?"}</span>
        <button type="button" class="link-btn" id="auth-switch-btn">${referralCode ? 'Log in' : 'Sign up'}</button>
      </p>
      <div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--gridline);${referralCode ? 'display:none;' : ''}" id="wallet-login-block">
        <button type="button" id="wallet-login-btn" style="width:100%;">Log in with wallet</button>
        <p class="muted" style="font-size:12px;margin:6px 0 0;">Only works if you've already linked a wallet from the Account page.</p>
        <div class="error-text" id="wallet-login-error" style="display:none;"></div>
      </div>
    </form>
  `;
  // Arriving via a referral link jumps straight to the signup side of the
  // form — someone who clicked an "invite a friend" link almost certainly
  // wants to sign up, not log in.
  let mode = referralCode ? 'signup' : 'login';
  const title = document.getElementById('auth-title');
  const switchText = document.getElementById('auth-switch-text');
  const switchBtn = document.getElementById('auth-switch-btn');
  const submitBtn = document.querySelector('#auth-form button.primary');
  const errorEl = document.getElementById('auth-error');
  const walletBlock = document.getElementById('wallet-login-block');
  const forgotPasswordRow = document.getElementById('forgot-password-row');
  const signupSubmitLabel = referralCode ? 'Sign up (get 10,250 play dollars)' : 'Sign up (get 10,000 play dollars)';

  switchBtn.onclick = () => {
    mode = mode === 'login' ? 'signup' : 'login';
    title.textContent = mode === 'login' ? 'Log in' : 'Sign up';
    submitBtn.textContent = mode === 'login' ? 'Log in' : signupSubmitLabel;
    switchText.textContent = mode === 'login' ? "Don't have an account?" : 'Already have an account?';
    switchBtn.textContent = mode === 'login' ? 'Sign up' : 'Log in';
    walletBlock.style.display = mode === 'login' ? 'block' : 'none';
    forgotPasswordRow.style.display = mode === 'login' ? 'block' : 'none';
  };

  document.getElementById('forgot-password-btn').onclick = () => navigate('#/forgot-password');

  async function applyLoginResult(result) {
    state.token = result.token;
    localStorage.setItem('pm_token', result.token);
    // The login/signup response itself doesn't carry daily_streak/
    // daily_bonus_claimable (those only exist on /api/me) — refetch here
    // instead of hand-assembling a partial state.user, so the header's
    // claim button/streak pill are correct immediately rather than only
    // after the next full page load.
    await refreshMe();
    pollNotifications();
    navigate('#/markets');
  }

  document.getElementById('auth-form').onsubmit = async (e) => {
    e.preventDefault();
    errorEl.style.display = 'none';
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    try {
      const body = mode === 'login' ? { username, password } : { username, password, referral_code: referralCode };
      const result = await api(mode === 'login' ? '/api/login' : '/api/signup', {
        method: 'POST', auth: false,
        body,
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

// ---------- forgot / reset password ----------

function renderForgotPassword() {
  appEl.innerHTML = `
    <form class="auth-form card" id="forgot-form">
      <h1>Forgot password?</h1>
      <p class="muted" style="margin-top:-8px;">Enter your username. If your account has an email on file, we'll send a reset link to it.</p>
      <label for="forgot-username">Username</label>
      <input type="text" id="forgot-username" required />
      <div class="error-text" id="forgot-error" style="display:none;"></div>
      <div class="muted" id="forgot-result" style="display:none;font-size:13px;margin-top:10px;"></div>
      <button class="primary" type="submit" style="width:100%;margin-top:14px;">Send reset link</button>
      <p style="margin-top:14px;font-size:13px;">
        <a href="#/login">Back to log in</a>
      </p>
    </form>
  `;
  const form = document.getElementById('forgot-form');
  const errorEl = document.getElementById('forgot-error');
  const resultEl = document.getElementById('forgot-result');
  form.onsubmit = async (e) => {
    e.preventDefault();
    errorEl.style.display = 'none';
    const username = document.getElementById('forgot-username').value.trim();
    const submitBtn = form.querySelector('button.primary');
    submitBtn.disabled = true;
    try {
      const result = await api('/api/forgot-password', { method: 'POST', auth: false, body: { username } });
      // Deliberately the same message no matter what the server actually
      // found (see the backend's own comment on this) — showing it as
      // plain confirmation text, not an error, either way.
      resultEl.textContent = result.detail;
      resultEl.style.display = 'block';
      form.querySelector('#forgot-username').disabled = true;
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = 'block';
      submitBtn.disabled = false;
    }
  };
}

function renderResetPassword() {
  const hashQuery = location.hash.split('?')[1] || '';
  const token = new URLSearchParams(hashQuery).get('token') || '';
  if (!token) {
    appEl.innerHTML = `<div class="empty-state">This reset link is missing its token. <a href="#/forgot-password">Request a new one</a>.</div>`;
    return;
  }
  appEl.innerHTML = `
    <form class="auth-form card" id="reset-form">
      <h1>Reset your password</h1>
      <label for="reset-password">New password</label>
      <input type="password" id="reset-password" required minlength="6" />
      <label for="reset-password-confirm">Confirm new password</label>
      <input type="password" id="reset-password-confirm" required minlength="6" />
      <div class="error-text" id="reset-error" style="display:none;"></div>
      <button class="primary" type="submit" style="width:100%;margin-top:14px;">Reset password</button>
    </form>
  `;
  const form = document.getElementById('reset-form');
  const errorEl = document.getElementById('reset-error');
  form.onsubmit = async (e) => {
    e.preventDefault();
    errorEl.style.display = 'none';
    const newPassword = document.getElementById('reset-password').value;
    const confirm = document.getElementById('reset-password-confirm').value;
    if (newPassword !== confirm) {
      errorEl.textContent = "Passwords don't match";
      errorEl.style.display = 'block';
      return;
    }
    try {
      await api('/api/reset-password', { method: 'POST', auth: false, body: { token, new_password: newPassword } });
      // Any old session (this browser or anywhere else) was just
      // invalidated server-side as part of the reset — clear whatever
      // token this tab happens to be holding too, so the UI doesn't lie
      // about being logged in, then send them to log in fresh.
      state.token = null;
      state.user = null;
      localStorage.removeItem('pm_token');
      appEl.innerHTML = `<div class="empty-state">Password reset — <a href="#/login">log in with your new password</a>.</div>`;
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = 'block';
    }
  };
}

// ---------- rules & FAQ ----------

const FAQ_ITEMS = [
  {
    q: 'Is this real money?',
    a: 'No — never. Every balance is play money that starts at $10,000 on signup. Nothing in UNICORN moves real dollars, real crypto, or anything else of real value, and there is currently no way to cash out. See the DEMO banner at the top of every page and the README for the full disclaimer.',
  },
  {
    q: 'How does pricing work?',
    a: 'Each market runs on an LMSR (Logarithmic Market Scoring Rule) automated market maker — the same style of mechanism real prediction-market platforms use. Buying YES shares pushes the YES price up; buying NO pushes it down. The "liquidity parameter" (b) controls how much a given trade moves the price — deeper liquidity means smaller price moves per trade.',
  },
  {
    q: 'How do markets resolve?',
    a: 'Timed markets (stocks, crypto, indices, commodities, forex, weather) settle automatically against a live feed once their clock runs out: above the strike reading at settlement pays out YES, at-or-below pays out NO. Sports markets skip the clock entirely — they resolve the moment a run scores, or NO once the half-inning ends scoreless, following the real MLB game. Manually created markets are resolved by an admin. Either way, every winning share pays exactly $1 and every losing share pays $0 — instantly, to every holder.',
  },
  {
    q: "What's the timed market roster?",
    a: 'A fixed, curated board: 12 top US stocks, the 10 most recognizable cryptocurrencies, the 4 headline US stock indices, 3 major commodities (gold, silver, crude oil), 3 major currency pairs, and 5 major-metro temperature readings — each running on a fast 5-minute or 15-minute "above or below this reading" clock, settling against real live market/weather data. Separately, while an MLB game is live, UNICORN also opens quick "will they score this half-inning" markets with no fixed clock — see the Sports category.',
  },
  {
    q: 'What are deposit fees?',
    a: `Deposits model a ${DEPOSIT_FEE_LABEL} fee on top of the amount you "deposit" — this is play money, so nothing is actually charged, but it mirrors the fee shape a real payment processor would take, so the economics are visible before any of this ever touches real funds.`,
  },
  {
    q: 'I forgot my password — what now?',
    a: 'Click "Forgot password?" on the login page and enter your username. If you\'ve added an email to your account from the Account page, we\'ll send a one-time reset link that expires in an hour. Haven\'t added an email yet? Add one from the Account page first — signup doesn\'t collect one by default, so there\'s nowhere to send a link until you do.',
  },
  {
    q: 'Can bots trade here?',
    a: 'Yes — generate an API key from the API keys page and trade programmatically using the same REST API the frontend uses. Full docs and a Python SDK live in API.md. Any account that ever places a trade with an API key shows up on the Bots tab of the Leaderboard from then on, separate from human traders.',
  },
  {
    q: 'What does "Connect wallet" actually do?',
    a: "It's login-only. Signing a one-time message with MetaMask (or another browser wallet) proves you control that address and links or authenticates your UNICORN account with it — no real crypto ever moves, and your balance stays UNICORN's own play money either way.",
  },
  {
    q: 'Is UNICORN registered with any regulator?',
    a: "No. UNICORN is a demo, not a registered exchange, broker, or money transmitter, and it doesn't offer real-money trading. It is not affiliated with Kalshi, Polymarket, or any other real trading platform.",
  },
];

function renderFaq() {
  appEl.innerHTML = `
    <h1>Rules &amp; FAQ</h1>
    <p class="muted">The short version of how UNICORN works, and what it is (and isn't).</p>
    <div class="faq-list">
      ${FAQ_ITEMS.map(item => `
        <details class="card faq-item">
          <summary>${escapeHtml(item.q)}</summary>
          <p>${item.a}</p>
        </details>
      `).join('')}
    </div>
  `;
}

// ---------- support banner ----------

const SUPPORT_BANNER_DISMISS_KEY = 'unicorn_support_banner_dismissed_v1';

function initSupportBanner() {
  const banner = document.getElementById('support-banner');
  if (!banner) return;
  try {
    if (localStorage.getItem(SUPPORT_BANNER_DISMISS_KEY) === '1') {
      banner.style.display = 'none';
      return;
    }
  } catch (err) {
    // localStorage can throw in some private-browsing modes — fall through
    // and just show the banner every visit rather than breaking the page.
  }
  const closeBtn = document.getElementById('support-banner-close');
  if (closeBtn) {
    closeBtn.onclick = () => {
      banner.style.display = 'none';
      try { localStorage.setItem(SUPPORT_BANNER_DISMISS_KEY, '1'); } catch (err) { /* ignore */ }
    };
  }
}

// ---------- install-to-home-screen banner ----------
//
// A free stand-in for a native App Store listing — this reuses the web app
// manifest (frontend/manifest.json) rather than needing an Apple Developer
// account. Two real install paths, handled differently because the
// underlying browser capability is different:
//   - Android/Chrome/Edge fire a `beforeinstallprompt` event when the page
//     qualifies (manifest + icons + served over HTTPS) — capturing that
//     event and replaying it on a button tap is the ONLY way to trigger a
//     real one-tap install; there's no way to invoke it out of the blue.
//   - iOS Safari never fires that event at all (Apple doesn't expose a
//     programmatic install prompt) — "Share > Add to Home Screen" is the
//     only path, so the best this can do is show instructions.
// Already-installed visits (standalone display mode, or iOS's
// navigator.standalone) skip the banner entirely — nothing to offer
// someone who already has it on their home screen.
const INSTALL_BANNER_DISMISS_KEY = 'unicorn_install_banner_dismissed_v1';
let deferredInstallPrompt = null;

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault(); // stop Chrome's own mini-infobar; this banner replaces it
  deferredInstallPrompt = e;
  showInstallBannerIfEligible();
});

function isRunningInstalled() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

function showInstallBannerIfEligible() {
  const banner = document.getElementById('install-banner');
  if (!banner || isRunningInstalled()) return;
  try {
    if (localStorage.getItem(INSTALL_BANNER_DISMISS_KEY) === '1') return;
  } catch (err) {
    // localStorage can throw in some private-browsing modes — fall through
    // and just show the banner every visit rather than breaking the page.
  }
  const textEl = document.getElementById('install-banner-text');
  const actionBtn = document.getElementById('install-banner-action');
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
  if (deferredInstallPrompt) {
    textEl.textContent = 'Add UNICORN to your home screen for a real app-like icon and full-screen launch — no App Store, no download.';
    actionBtn.style.display = '';
    actionBtn.onclick = async () => {
      actionBtn.disabled = true;
      deferredInstallPrompt.prompt();
      await deferredInstallPrompt.userChoice; // resolves once the user accepts/dismisses Chrome's own dialog
      deferredInstallPrompt = null;
      banner.style.display = 'none';
      try { localStorage.setItem(INSTALL_BANNER_DISMISS_KEY, '1'); } catch (err) { /* ignore */ }
    };
  } else if (isIOS) {
    textEl.textContent = 'Add UNICORN to your home screen: tap the Share icon, then "Add to Home Screen".';
    actionBtn.style.display = 'none';
  } else {
    return; // desktop browser with no install offer and not iOS — nothing useful to show
  }
  banner.style.display = '';
}

function initInstallBanner() {
  const closeBtn = document.getElementById('install-banner-close');
  if (closeBtn) {
    closeBtn.onclick = () => {
      document.getElementById('install-banner').style.display = 'none';
      try { localStorage.setItem(INSTALL_BANNER_DISMISS_KEY, '1'); } catch (err) { /* ignore */ }
    };
  }
  // iOS never fires beforeinstallprompt, so it has to be checked eagerly
  // here rather than only from that event's listener above.
  showInstallBannerIfEligible();
}

// ---------- header activity pulse (sweep speeds up with site-wide trading) ----------
//
// Purely cosmetic, page-chrome-level state — lives outside the router, so
// it's a plain persistent setInterval rather than one registered with
// trackInterval() (which the router clears on every navigation; the header
// bars span every page, so this shouldn't stop just because the route did).

const ACTIVITY_POLL_MS = 5000;
const SWEEP_DURATION_IDLE_S = 5; // matches the CSS fallback — zero recent trades, same pace as before this existed
const SWEEP_DURATION_MIN_S = 1.1; // floor so it never spins fast enough to look broken/flickery

function updateSweepSpeed(tradesLast60s) {
  const seconds = Math.max(SWEEP_DURATION_MIN_S, SWEEP_DURATION_IDLE_S - tradesLast60s * 0.35);
  document.documentElement.style.setProperty('--sweep-duration', `${seconds.toFixed(2)}s`);
}

async function pollActivity() {
  try {
    const { trades_last_60s } = await api('/api/activity', { auth: false });
    updateSweepSpeed(trades_last_60s);
  } catch (e) {
    // Decorative only — a failed poll just leaves the sweep at its last
    // known speed (or the CSS default) rather than surfacing an error.
  }
}

// ---------- boot ----------

(async function boot() {
  initSupportBanner();
  initInstallBanner();
  pollActivity();
  setInterval(pollActivity, ACTIVITY_POLL_MS);
  setInterval(pollNotifications, NOTIF_POLL_MS);
  await refreshMe();
  renderHeader();
  pollNotifications();
  navigate();
})();
