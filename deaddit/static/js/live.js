// Live activity page: socket-driven auto-update. New events are prepended the
// moment they arrive; the "N new events" pill doubles as a fallback (paused
// mode, failed swaps, no-JS full navigation) plus the htmx-powered append
// (older) swap. Degrades silently: without a socket the page stays fully
// usable via the plain links.
//
// Kind filtering is server-side (?kinds=), so this file only has to carry the
// active selection onto the URLs it rewrites — see kindsQuery.

const page = document.querySelector('.live-page');
const list = document.getElementById('live-list');
const pill = document.getElementById('live-newer');
const pauseBtn = document.getElementById('live-pause');
const pauseLabel = document.getElementById('live-pause-label');
const errorEl = document.getElementById('live-error');
const dot = document.getElementById('live-dot');
const stateEl = document.getElementById('live-state');

// Retention cap: an always-on ticker would otherwise grow the DOM without
// bound over a long session. Older rows are dropped from the tail as new ones
// arrive (see trimOverflow), and the "load older" cursor is re-anchored to
// whatever row is last afterwards so paging stays gapless.
const MAX_ITEMS = 200;

const kindsQuery = (page && page.dataset.kindsQuery) || '';

let paused = false;
let pendingCount = 0;
let socket = null;
let loadInFlight = false; // an htmx prepend is fetching the pending events
let lastLoadAt = 0; // an in-flight live_count emitted just before our ack can
                    // arrive right after a load; ignore those stragglers.

// The bar sticks below the site header, whose height is chrome we do not want
// to hard-code in two places.
function syncStickyOffset() {
    const header = document.getElementById('siteHeader');
    if (!header || !page) return;
    page.style.setProperty('--live-sticky-top', header.offsetHeight + 'px');
}

syncStickyOffset();
window.addEventListener('resize', syncStickyOffset);

function setState(state, label) {
    if (dot) dot.dataset.state = state;
    if (stateEl) stateEl.textContent = label;
}

function newestBy(attr) {
    const first = document.querySelector('#live-list li[data-cursor]');
    return first ? first.dataset[attr] : '';
}

function sinceUrl(cursor, fragment) {
    return '/live?since=' + encodeURIComponent(cursor) + kindsQuery +
        (fragment ? '&fragment=1' : '');
}

function syncPillUrl() {
    const cursor = newestBy('cursor');
    if (!cursor || !pill) return;
    // No-JS fallback navigates to a full page of what's new; htmx takes the
    // fragment (bare <li> nodes) for the prepend swap.
    pill.setAttribute('href', sinceUrl(cursor, false));
    pill.setAttribute('hx-get', sinceUrl(cursor, true));
}

// Rows arriving after first paint get a one-shot arrival flash; everything
// present at load is marked seen so the page does not light up on entry.
function markSeen(flash) {
    if (!list) return;
    list.querySelectorAll(':scope > li:not([data-seen])').forEach((el) => {
        el.dataset.seen = '1';
        if (flash) el.classList.add('live-item--fresh');
    });
}

function reanchorOlder() {
    const older = document.getElementById('live-older');
    const last = list && list.querySelector(':scope > li:last-child');
    if (!older || !last || !last.dataset.cursor) return;
    const url = '/live?before=' + encodeURIComponent(last.dataset.cursor) + kindsQuery;
    older.setAttribute('href', url);
    older.setAttribute('hx-get', url);
    if (window.htmx) window.htmx.process(older);
}

function trimOverflow() {
    if (!list) return;
    const items = list.querySelectorAll(':scope > li');
    let removed = false;
    for (let i = items.length - 1; i >= MAX_ITEMS; i--) {
        // Never yank a row the reader can still see — the cap re-applies on
        // the next arrival, once the row has scrolled out of the way.
        if (items[i].getBoundingClientRect().top < window.innerHeight) break;
        items[i].remove();
        removed = true;
    }
    if (removed) reanchorOlder();
}

function showError() {
    loadInFlight = false; // a failed swap leaves events pending; the pump
                          // re-emits each tick so the next count retries it
    if (errorEl) errorEl.hidden = false;
}

// Socket unavailable (client missing, connect failure): hide live-only
// controls; the plain links keep the page fully usable.
function degrade() {
    if (pill) pill.hidden = true;
    if (pauseBtn) pauseBtn.hidden = true;
    try { if (socket) socket.disconnect(); } catch (_) { /* noop */ }
    socket = null;
    setState('idle', 'auto-refresh off');
}

markSeen(false);

if (list && pill) {
    try {
        if (typeof window.io !== 'function') throw new Error('socket.io client unavailable');
        socket = window.io('/live', { transports: ['polling', 'websocket'] });
        socket.on('connect', () => {
            socket.emit('join_activity');
            if (pauseBtn) pauseBtn.hidden = false;
            if (!paused) setState('live', 'streaming');
        });
        socket.on('disconnect', () => setState('idle', 'reconnecting…'));
        socket.on('live_count', (data) => {
            // Straggler guard: counts emitted before our just-sent ack.
            if (Date.now() - lastLoadAt < 1500) return;
            pendingCount = Number(data && data.count) || 0;
            if (!pill) return;
            if (pendingCount <= 0) {
                pill.hidden = true;
                return;
            }
            if (paused) {
                // Paused: show the click-to-load badge; the server re-emits
                // each tick so the count stays fresh until resume.
                pill.textContent = pendingCount + ' new ' +
                    (pendingCount === 1 ? 'event' : 'events') + ' — click to load';
                pill.hidden = false;
                return;
            }
            // Live mode: load the new events the moment they arrive instead
            // of waiting for a click. The pill still appears briefly and
            // stays clickable as a fallback if a swap ever fails.
            if (loadInFlight) return; // a fetch already covers these events
            pill.textContent = pendingCount + ' new ' +
                (pendingCount === 1 ? 'event' : 'events');
            pill.hidden = false;
            syncPillUrl();
            loadInFlight = true;
            if (window.htmx) {
                window.htmx.trigger(pill, 'click');
            } else {
                pill.click(); // no htmx: plain link (full navigation)
            }
        });
        socket.on('connect_error', degrade);
    } catch (_) {
        degrade();
    }

    window.addEventListener('pagehide', () => {
        if (socket) socket.emit('leave_activity');
    });
} else {
    degrade();
}

if (pauseBtn && pill) {
    pauseBtn.addEventListener('click', () => {
        paused = !paused;
        pauseBtn.setAttribute('aria-pressed', String(paused));
        if (pauseLabel) pauseLabel.textContent = paused ? 'Resume' : 'Pause';
        const icon = pauseBtn.querySelector('i');
        if (icon) icon.className = paused ? 'bi bi-play-fill' : 'bi bi-pause-fill';
        setState(paused ? 'paused' : 'live', paused ? 'paused' : 'streaming');
        if (paused) pill.hidden = true;
    });
}

// htmx swaps drive both controls so app.js relative-time upgrade keeps
// working via htmx:afterSwap.

document.body.addEventListener('htmx:afterSwap', (e) => {
    // e.detail.elt is the swap TARGET (#live-list); the control that fired
    // the request lives on requestConfig.elt.
    const elt = (e.detail.requestConfig && e.detail.requestConfig.elt) || e.detail.elt;
    if (errorEl) errorEl.hidden = true;
    if (elt && elt.id === 'live-older') {
        markSeen(false);
        return;
    }
    if (elt && elt.id === 'live-newer') {
        loadInFlight = false;
        lastLoadAt = Date.now();
        pendingCount = 0;
        pill.hidden = true;
        syncPillUrl();
        // Refresh htmx's cached path after the attribute change.
        if (window.htmx) window.htmx.process(pill);
        markSeen(true);
        trimOverflow();
        // The server hands back the GLOBAL newest timestamp out-of-band, so a
        // filtered view still acks past events it chose not to render; without
        // it the pump's watermark would stall and re-report them forever.
        const watermark = document.getElementById('live-watermark');
        const ts = (watermark && watermark.dataset.ts) || newestBy('ts');
        if (socket && ts) socket.emit('activity_loaded', { ts: ts });
    }
});

document.body.addEventListener('htmx:responseError', showError);
document.body.addEventListener('htmx:sendError', showError);
document.body.addEventListener('htmx:timeout', showError);
