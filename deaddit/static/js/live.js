// Live activity page (UX-6 Slice B): socket-driven auto-update. New events
// are prepended the moment they arrive; the "N new events" pill doubles as a
// fallback (paused mode, failed swaps, no-JS full navigation) plus the
// htmx-powered append (older) swap. Degrades silently: without a socket the
// page stays fully usable via the plain links.

const list = document.getElementById('live-list');
const pill = document.getElementById('live-newer');
const pauseBtn = document.getElementById('live-pause');
const errorEl = document.getElementById('live-error');

let paused = false;
let pendingCount = 0;
let socket = null;
let loadInFlight = false; // an htmx prepend is fetching the pending events
let lastLoadAt = 0; // an in-flight live_count emitted just before our ack can
                    // arrive right after a load; ignore those stragglers.

function newestBy(attr) {
    const first = document.querySelector('#live-list li[data-cursor]');
    return first ? first.dataset[attr] : '';
}

function syncPillUrl() {
    const cursor = newestBy('cursor');
    if (!cursor || !pill) return;
    // No-JS fallback navigates to a full page of what's new; htmx takes the
    // fragment (bare <li> nodes) for the prepend swap.
    pill.setAttribute('href', '/live?since=' + encodeURIComponent(cursor));
    pill.setAttribute('hx-get', '/live?since=' + encodeURIComponent(cursor) + '&fragment=1');
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
}

if (list && pill) {
    try {
        if (typeof window.io !== 'function') throw new Error('socket.io client unavailable');
        socket = window.io('/live', { transports: ['polling', 'websocket'] });
        socket.on('connect', () => {
            socket.emit('join_activity');
            if (pauseBtn) pauseBtn.hidden = false;
        });
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
        pauseBtn.textContent = paused ? 'Resume' : 'Pause';
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
    if (elt && elt.id === 'live-newer') {
        loadInFlight = false;
        lastLoadAt = Date.now();
        pendingCount = 0;
        pill.hidden = true;
        syncPillUrl();
        // Refresh htmx's cached path after the attribute change.
        if (window.htmx) window.htmx.process(pill);
        const ts = newestBy('ts');
        if (socket && ts) socket.emit('activity_loaded', { ts: ts });
    }
});

document.body.addEventListener('htmx:responseError', showError);
document.body.addEventListener('htmx:sendError', showError);
document.body.addEventListener('htmx:timeout', showError);
