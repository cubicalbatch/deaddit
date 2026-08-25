// Live activity page (UX-6 Slice B): socket-driven "N new events" pill plus
// htmx-powered prepend (newer) / append (older) swaps. Degrades silently:
// without a socket the page stays fully usable via the plain links.

const list = document.getElementById('live-list');
const pill = document.getElementById('live-newer');
const pauseBtn = document.getElementById('live-pause');
const errorEl = document.getElementById('live-error');

let paused = false;
let pendingCount = 0;
let socket = null;
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
            if (!paused && pendingCount > 0) {
                pill.textContent = pendingCount + ' new ' +
                    (pendingCount === 1 ? 'event' : 'events') + ' — click to load';
                pill.hidden = false;
            } else if (pendingCount <= 0) {
                pill.hidden = true;
            }
            // While paused, leave the badge untouched until resume; the server
            // re-emits each tick so the count converges afterwards.
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
    const elt = e.detail && e.detail.elt;
    if (errorEl) errorEl.hidden = true;
    if (elt && elt.id === 'live-newer') {
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
