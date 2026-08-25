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
