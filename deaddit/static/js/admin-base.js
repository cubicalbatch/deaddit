// Deaddit admin base behavior (vanilla JS, no dependencies).
// Loaded deferred after vendor bundles.

// Theme toggle - same id/class contract as the public site header
// (templates/base.html + static/js/app.js).
function syncToggle() {
    const themeToggle = document.getElementById('themeToggle');
    if (!themeToggle) return;
    const dark = document.documentElement.dataset.theme === 'dark';
    themeToggle.setAttribute('aria-pressed', String(dark));
    themeToggle.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
    const icon = themeToggle.querySelector('i');
    if (icon) {
        icon.classList.toggle('bi-moon-fill', !dark);
        icon.classList.toggle('bi-sun-fill', dark);
    }
}

const themeToggleButton = document.getElementById('themeToggle');
if (themeToggleButton) {
    themeToggleButton.addEventListener('click', () => {
        const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        document.documentElement.dataset.theme = next;
        localStorage.setItem('theme', next);
        syncToggle();
    });
}
syncToggle();


