// Per-image expand/minimize for image post cards (plan 6C).
// Vanilla ES module, no dependencies. Loaded globally from base.html since
// image cards can appear on the front page, subdeaddits, user profiles, and
// search results.
//
// Expand/minimize buttons are injected here rather than rendered server-side:
// post_card.html already ships the complete no-JS experience (a correctly
// sized thumbnail), and a button that does nothing without JS has no
// business in the served markup. The one thing the template DOES add is a
// `data-original-src` attribute on the thumbnail - inert data this script
// needs (the full-image URL), not a control.
//
// All click handling is delegated to `document`, registered once at module
// load. That single listener keeps working for every card HTMX appends
// later (Load More), so there is never a second listener to accumulate.

const EXPAND_ICON = 'bi-arrows-angle-expand';
const MINIMIZE_ICON = 'bi-arrows-angle-contract';

let nextImageId = 0;

function cardImageAndButton(media) {
    return [media.querySelector('.post-card__thumb'), media.querySelector('.post-card__expand')];
}

function isExpanded(media) {
    const btn = media.querySelector('.post-card__expand');
    return btn ? btn.getAttribute('aria-expanded') === 'true' : false;
}

function setCardState(media, expanded) {
    const [img, btn] = cardImageAndButton(media);
    if (!img || !btn) return;
    const targetSrc = expanded ? img.dataset.originalSrc : img.dataset.thumbSrc;
    if (!targetSrc) return;

    if (img.getAttribute('src') !== targetSrc) img.src = targetSrc;
    img.classList.toggle('is-expanded', expanded);
    btn.setAttribute('aria-expanded', String(expanded));
    btn.setAttribute('aria-label', expanded ? 'Minimize image' : 'Expand image');
    const icon = btn.querySelector('i');
    if (icon) {
        icon.classList.toggle(EXPAND_ICON, !expanded);
        icon.classList.toggle(MINIMIZE_ICON, expanded);
    }
}

// Adds the expand/minimize control to one card's media block, unless it
// already has one (idempotent so repeated htmx:afterSwap/load passes over
// already-enhanced cards are harmless no-ops).
function enhanceCard(media) {
    if (media.dataset.imageEnhanced) return;
    const img = media.querySelector('.post-card__thumb');
    // No data-original-src means nothing to expand into; leave the plain
    // thumbnail alone rather than adding a button that can't do its job.
    if (!img || !img.dataset.originalSrc) return;
    media.dataset.imageEnhanced = 'true';

    if (!img.id) img.id = `post-image-${++nextImageId}`;
    img.dataset.thumbSrc = img.getAttribute('src');

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'post-card__expand';
    btn.setAttribute('data-image-toggle', '');
    btn.setAttribute('aria-controls', img.id);
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-label', 'Expand image');
    btn.innerHTML = `<i class="bi ${EXPAND_ICON}" aria-hidden="true"></i>`;
    media.appendChild(btn);
}

function enhanceAll(root = document) {
    root.querySelectorAll('.post-card__media').forEach(enhanceCard);
}

document.addEventListener('click', (event) => {
    const toggleBtn = event.target.closest('[data-image-toggle]');
    if (toggleBtn) {
        const media = toggleBtn.closest('.post-card__media');
        if (media) setCardState(media, !isExpanded(media));
    }
});

enhanceAll();
document.addEventListener('DOMContentLoaded', () => enhanceAll());
// New cards appended by htmx (Load More) get the same treatment.
for (const event of ['htmx:afterSwap', 'htmx:load']) {
    document.addEventListener(event, () => enhanceAll());
}
