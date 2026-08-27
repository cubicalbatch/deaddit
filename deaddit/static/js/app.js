// Deaddit client behavior (vanilla ES module, no dependencies)

function syncToggle() {
    const toggle = document.getElementById('themeToggle');
    if (!toggle) return;
    const dark = document.documentElement.dataset.theme === 'dark';
    toggle.setAttribute('aria-pressed', String(dark));
    toggle.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
    const icon = toggle.querySelector('i');
    if (icon) {
        icon.classList.toggle('bi-moon-fill', !dark);
        icon.classList.toggle('bi-sun-fill', dark);
    }
}

syncToggle();

const toggle = document.getElementById('themeToggle');
if (toggle) {
    toggle.addEventListener('click', () => {
        const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        document.documentElement.dataset.theme = next;
        localStorage.setItem('theme', next);
        localStorage.removeItem('nightMode');
        syncToggle();
    });
}

// Relative-time upgrade: every <time datetime> gets "3h ago" style text and
// an absolute locale string as title. The ISO datetime attribute is kept.
function formatRelative(ms) {
    const units = [
        ['y', 365 * 24 * 60 * 60 * 1000],
        ['mo', 30 * 24 * 60 * 60 * 1000],
        ['d', 24 * 60 * 60 * 1000],
        ['h', 60 * 60 * 1000],
        ['m', 60 * 1000],
    ];
    for (const [suffix, size] of units) {
        if (ms >= size) return `${Math.floor(ms / size)}${suffix} ago`;
    }
    return ms >= 45 * 1000 ? 'just now' : 'now';
}

// Server timestamps are naive UTC ISO strings (no offset). The ES2015+ spec
// parses an offset-less date-time as LOCAL time, which skews every relative
// time by the client's UTC offset; assume UTC when no offset is present.
function parseServerTime(value) {
    if (!value) return NaN;
    if (value.indexOf('T') !== -1 && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(value)) {
        value += 'Z';
    }
    return Date.parse(value);
}

function upgradeTimes() {
    const now = Date.now();
    document.querySelectorAll('time[datetime]').forEach((el) => {
        const ts = parseServerTime(el.getAttribute('datetime'));
        if (Number.isNaN(ts)) return;
        el.title = new Date(ts).toLocaleString();
        el.textContent = ts <= now ? formatRelative(now - ts) : 'in ' + formatRelative(ts - now);
    });
}

upgradeTimes();
document.addEventListener('DOMContentLoaded', upgradeTimes);
// New content appended by htmx (load-more feed pages, swapped fragments)
for (const event of ['htmx:afterSwap', 'htmx:load']) {
    document.addEventListener(event, () => upgradeTimes());
}
