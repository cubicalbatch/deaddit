// Deaddit comment tree behaviors (UX-3). Vanilla ES module, no dependencies.
// Loaded deferred from post.html alongside the global app.js (relative times).

const STORAGE_KEY = 'deaddit.collapsed';

// ---------------------------------------------------------------- storage ---

function loadCollapsed() {
    try {
        const raw = sessionStorage.getItem(STORAGE_KEY);
        if (!raw) return null; // no session state yet: server flags stay in charge
        const parsed = JSON.parse(raw);
        return new Set(Array.isArray(parsed) ? parsed.map(String) : []);
    } catch {
        return null;
    }
}

function saveCollapsed(ids) {
    try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...ids]));
    } catch {
        // Private mode / quota: collapse still works, it just won't persist.
    }
}

// Session state becomes authoritative from the first user toggle onward:
// we snapshot every currently-collapsed comment id so a reload never fights
// the server-set auto-collapse defaults ("session wins").
function snapshotCollapsed() {
    const ids = new Set();
    document.querySelectorAll('.comment.is-collapsed[data-comment-id]').forEach((el) => {
        ids.add(el.dataset.commentId);
    });
    saveCollapsed(ids);
}

// Deep links are visits, not user toggles: they must never CREATE a
// snapshot (that would freeze this page's server-set auto-collapse for the
// whole session). They only drop ids we force-expanded from an EXISTING
// snapshot, so a reload re-applies server auto-collapse for them.
function pruneExpanded(ids) {
    if (!ids.size) return;
    const stored = loadCollapsed();
    if (!stored) return;
    let changed = false;
    for (const id of ids) {
        if (stored.delete(id)) changed = true;
    }
    if (changed) saveCollapsed(stored);
}

// ------------------------------------------------------- collapse toggling --

function collapseButton(comment) {
    return comment.querySelector(':scope > .comment-collapse');
}

function setAria(comment, expanded) {
    const btn = collapseButton(comment);
    if (!btn || btn.getAttribute('aria-expanded') === String(expanded)) return false;
    btn.setAttribute('aria-expanded', String(expanded));
    const link = comment.querySelector(':scope > .comment-main > .comment-actions > .comment-collapse-link');
    if (link) {
        link.setAttribute('aria-expanded', String(expanded));
        const n = Number(comment.dataset.descendants || 0);
        // Only the label span: the button also holds a chevron element that
        // a textContent write on the button itself would destroy.
        const label = link.querySelector('.collapse-link__label') || link;
        label.textContent = expanded
            ? `Hide ${n} ${n === 1 ? 'reply' : 'replies'}`
            : `Show ${n} ${n === 1 ? 'reply' : 'replies'}`;
    }
    // aria-label tracks the state so screen readers announce the action,
    // not the state ("Collapse thread…" vs "Expand thread…").
    const m = btn.getAttribute('aria-label');
    if (m) {
        btn.setAttribute('aria-label', m.replace(/^(Expand|Collapse)/, expanded ? 'Collapse' : 'Expand'));
    }
    return true;
}

// Nested mode: CSS hides .comment-body and .comment-children under .is-collapsed.
function toggleNested(comment) {
    const collapsed = comment.classList.toggle('is-collapsed');
    setAria(comment, !collapsed);
    return collapsed;
}

// Flat mode (.comment--flat): hide this node's body AND every following flat
// sibling with greater data-depth until a sibling at depth <= mine.
function flatHiddenSiblings(comment) {
    const depth = Number(comment.dataset.depth || 0);
    const hidden = [];
    let sib = comment.nextElementSibling;
    while (sib && sib.classList.contains('comment--flat')) {
        if (Number(sib.dataset.depth || 0) <= depth) break;
        hidden.push(sib);
        sib = sib.nextElementSibling;
    }
    return hidden;
}

function toggleFlat(comment) {
    const collapsed = comment.classList.toggle('is-collapsed');
    setAria(comment, !collapsed);

    for (const sib of flatHiddenSiblings(comment)) {
        sib.classList.toggle('flat-hidden', collapsed);
    }
    if (collapsed) {
        // Recount from the DOM instead of trusting descendant_count: some
        // siblings may have been individually hidden by an inner collapse.
        const hidden = [...flatHiddenSiblings(comment)]
            .filter((sib) => sib.classList.contains('flat-hidden')).length;
        updatePill(comment, hidden);
    }
    return collapsed;
}

function expandFlatChain(comment) {
    const hiddenSiblings = flatHiddenSiblings(comment);
    comment.classList.remove('is-collapsed');
    setAria(comment, true);
    for (const sib of hiddenSiblings) sib.classList.remove('flat-hidden');
    return [comment, ...hiddenSiblings];
}

// ------------------------------------------------- hidden-replies summaries --

const NAME_LIMIT = 3;

function directChildNames(comment) {
    const kids = comment.querySelector(':scope > .comment-children');
    if (!kids) return [];
    // Removed children render a tombstone without a user link, so the
    // selector naturally drops them; deeper generations are covered by count.
    return [...kids.querySelectorAll(':scope > .comment > .comment-main > .comment-header > .comment-meta__user')]
        .map((el) => el.textContent.trim())
        .slice(0, NAME_LIMIT + 1);
}

function updatePill(comment, hiddenCount) {
    const pill = comment.querySelector('.comment-replies-pill');
    if (!pill) return;
    const countEl = pill.querySelector('.pill-count');
    const namesEl = pill.querySelector('.pill-names');
    if (countEl && hiddenCount != null) {
        countEl.textContent = `${hiddenCount} hidden`;
    }
    if (namesEl && hiddenCount != null && hiddenCount > 0) {
        // Recompute from live DOM: children already expanded by the user drop
        // out of the summary; deeper generations are covered by the count.
        const all = directChildNames(comment);
        const names = all.slice(0, NAME_LIMIT);
        namesEl.textContent = names.length ? ` — ${names.join(', ')}${all.length > NAME_LIMIT ? '…' : ''}` : '';
    }
}

// --------------------------------------------------------- sticky offset ---

// Height of the chrome that floats over the thread: the site header, plus
// the comments toolbar once it has stuck to it. Anything we scroll to has to
// clear both or it lands underneath them.
function stickyOffset() {
    const header = document.querySelector('.site-header');
    const toolbar = document.querySelector('.comments-toolbar');
    const headerH = header ? header.getBoundingClientRect().height : 0;
    const toolbarH = toolbar ? toolbar.getBoundingClientRect().height : 0;
    return headerH + toolbarH;
}

// ------------------------------------------------------------- toggling ----

// Collapsing a tall thread can leave its own header scrolled off the top —
// the reader clicks a rail and the page appears to jump to unrelated
// content. Pull the collapsed row back under the sticky chrome when that
// happens; never scroll on expand, which keeps the anchor row where it is.
function keepRowInView(comment) {
    const top = comment.getBoundingClientRect().top;
    const limit = stickyOffset() + 8;
    if (top < limit) window.scrollBy({ top: top - limit, behavior: 'auto' });
}

function toggleComment(btn) {
    const comment = btn.closest('.comment');
    if (!comment) return;
    if (comment.classList.contains('is-collapsed')) {
        if (comment.classList.contains('comment--flat')) expandFlatChain(comment);
        else toggleNested(comment);
        updatePill(comment, null);
    } else {
        let hiddenCount = null;
        if (comment.classList.contains('comment--flat')) {
            toggleFlat(comment);
            hiddenCount = [...flatHiddenSiblings(comment)].filter((sib) => sib.classList.contains('flat-hidden')).length;
        } else {
            toggleNested(comment);
            hiddenCount = Number(comment.dataset.descendants || 0);
        }
        updatePill(comment, hiddenCount);
        keepRowInView(comment);
    }
    snapshotCollapsed();
    refreshCollapseAll();
}

// ------------------------------------------------------------ hover chain ---

let chainMembers = [];
function clearChain() {
    for (const el of chainMembers) el.classList.remove('chain');
    chainMembers = [];
}

document.addEventListener('mouseover', (event) => {
    clearChain();
    const comment = event.target.closest('.comment');
    if (!comment) return;
    let node = comment.parentElement ? comment.parentElement.closest('.comment') : null;
    while (node) {
        node.classList.add('chain');
        chainMembers.push(node);
        node = node.parentElement ? node.parentElement.closest('.comment') : null;
    }
});
document.addEventListener('mouseout', clearChain);

// ------------------------------------------------------------- deep links ---

function expandAncestors(target) {
    const expanded = [];
    let node = target.parentElement ? target.parentElement.closest('.comment') : null;
    while (node) {
        expanded.push(node);
        if (node.classList.contains('comment--flat')) expanded.push(...expandFlatChain(node));
        else {
            node.classList.remove('is-collapsed');
            setAria(node, true);
        }
        node = node.parentElement ? node.parentElement.closest('.comment') : null;
    }
    return expanded;
}

let highlightTimer = null;
function handleHash() {
    const match = location.hash.match(/^#comment-(\d+)$/);
    if (!match) return;
    const target = document.getElementById(`comment-${match[1]}`);
    if (!target) return;

    // Ancestors first, then the target itself (it may be auto-collapsed or
    // flat-hidden by a collapsed ancestor).
    const expanded = expandAncestors(target);
    const flatParent = target.parentElement ? target.parentElement.closest('.comment--flat') : null;
    if (flatParent) expanded.push(...expandFlatChain(flatParent));
    // A flat target can also be hidden by an earlier COLLAPSED FLAT SIBLING
    // (flat mode hides deeper following siblings, not descendants). Expand
    // every sibling whose hidden span covers the target.
    if (target.classList.contains('comment--flat')) {
        let sib = target.previousElementSibling;
        while (sib && sib.classList.contains('comment--flat')) {
            if (sib.classList.contains('is-collapsed') && flatHiddenSiblings(sib).includes(target)) {
                expanded.push(...expandFlatChain(sib));
            }
            sib = sib.previousElementSibling;
        }
    }
    if (target.classList.contains('comment--flat') && target.classList.contains('flat-hidden')) {
        target.classList.remove('flat-hidden');
        expanded.push(target);
    }
    if (target.classList.contains('is-collapsed')) {
        expanded.push(target);
        if (target.classList.contains('comment--flat')) expandFlatChain(target);
        else toggleNested(target);
    }
    pruneExpanded(new Set(
        expanded
            .map((el) => el.dataset.commentId)
            .filter(Boolean)
    ));

    target.scrollIntoView({ block: 'center' });
    target.classList.add('permalink-highlight');
    clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => target.classList.remove('permalink-highlight'), 2000);
}

window.addEventListener('hashchange', handleHash);

// -------------------------------------------------------------- sorting ----

// Re-sorting is done in the page, not by navigating: every comment is already
// in the DOM, so a reload only costs a round trip and throws the reader back
// to the top of the document. The server still renders the ?sort= order (it
// is the no-JS path and what a shared link reproduces), and publishes the
// numbers it ranked with as data-rank-*, so the two orders are identical.
//
// Every comment sort reduces to "metric DESC, id DESC" — `new` included,
// since a later timestamp is a larger number — so one comparator covers all
// four. See rank_metrics() in routes.py.
const SORTS = ['best', 'top', 'new', 'controversial'];

function rankOf(comment, sort) {
    return Number(comment.dataset[`rank${sort[0].toUpperCase()}${sort.slice(1)}`] || 0);
}

// Narrow screens scroll the sort bar horizontally rather than wrapping the
// toolbar onto a second sticky row; the selected option has to stay visible
// inside it. scrollIntoView would also scroll the page, so nudge the bar's
// own scrollLeft instead.
function revealActiveSort(sortBar) {
    const active = sortBar.querySelector('.sort-bar__link.is-active');
    if (!active || sortBar.scrollWidth <= sortBar.clientWidth) return;
    const left = active.offsetLeft - (sortBar.clientWidth - active.offsetWidth) / 2;
    sortBar.scrollLeft = Math.max(0, left);
}

function reorderGroup(container, sort) {
    const items = [...container.children].filter((el) => el.classList.contains('comment'));
    if (items.length < 2) return;
    items.sort(
        (a, b) =>
            rankOf(b, sort) - rankOf(a, sort) ||
            Number(b.dataset.commentId) - Number(a.dataset.commentId),
    );
    // One reflow: appending to a fragment detaches each node from the
    // container, and the fragment goes back in a single insertion.
    const frag = document.createDocumentFragment();
    for (const item of items) frag.appendChild(item);
    container.appendChild(frag);
}

function applySort(sort, sortBar) {
    const tree = document.querySelector('.comments-tree');
    if (!tree) return;

    // Sibling groups only: roots, then each nested reply list. Flattened
    // tails below the depth cap are deliberately skipped — they are one
    // in-order projection of a subtree, and reordering those rows would
    // scramble which reply belongs under which.
    reorderGroup(tree, sort);
    for (const group of tree.querySelectorAll('.comment-children')) {
        reorderGroup(group, sort);
    }

    for (const link of sortBar.querySelectorAll('.sort-bar__link')) {
        const active = link.dataset.sort === sort;
        link.classList.toggle('is-active', active);
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    }
    revealActiveSort(sortBar);

    // Keep the address bar honest so a copied link reproduces this order.
    const url = new URL(location.href);
    url.searchParams.set('sort', sort);
    url.hash = '';
    history.replaceState(history.state, '', url);

    // Scroll rule: a re-sort answers "show me this thread another way", so
    // land on the first comment of the new order — but only if the reader
    // had already scrolled into the discussion. Reading the post itself is
    // never interrupted, and nothing ever jumps to the top of the page.
    const section = document.getElementById('comments');
    if (section) {
        const header = document.querySelector('.site-header');
        const headerH = header ? header.getBoundingClientRect().height : 0;
        const top = section.getBoundingClientRect().top;
        if (top < headerH) {
            window.scrollTo({ top: window.scrollY + top - headerH, behavior: 'auto' });
        }
    }

    announce(`Comments sorted by ${sort}`);
}

// ------------------------------------------------------------ collapse all --

function rootComments() {
    const tree = document.querySelector('.comments-tree');
    return tree ? [...tree.children].filter((el) => el.classList.contains('comment')) : [];
}

function setCollapseAll(collapsed) {
    if (collapsed) {
        // Roots only: collapsing a root already hides everything under it,
        // and leaving inner state alone means expanding all is a clean undo.
        for (const comment of rootComments()) {
            if (!comment.classList.contains('is-collapsed')) {
                const btn = collapseButton(comment);
                if (btn) toggleComment(btn);
            }
        }
        const first = rootComments()[0];
        if (first) keepRowInView(first);
    } else {
        for (const comment of document.querySelectorAll('.comment.is-collapsed')) {
            const btn = collapseButton(comment);
            if (btn) toggleComment(btn);
        }
    }
    snapshotCollapsed();
}

function syncCollapseAll(btn) {
    const roots = rootComments();
    const allCollapsed = roots.length > 0 && roots.every((el) => el.classList.contains('is-collapsed'));
    btn.setAttribute('aria-pressed', String(allCollapsed));
    const text = allCollapsed ? 'Expand all' : 'Collapse all';
    const label = btn.querySelector('.collapse-all__label');
    if (label) label.textContent = text;
    // The label is visually hidden on narrow screens, so the accessible name
    // and the tooltip have to carry the state on their own.
    btn.setAttribute('aria-label', `${text} comments`);
    btn.setAttribute('title', `${text} comments`);
}

// The button also has to follow along when threads are collapsed one at a
// time, so it never offers "Collapse all" on an already-closed tree.
function refreshCollapseAll() {
    const btn = document.querySelector('[data-collapse-all]');
    if (btn) syncCollapseAll(btn);
}

// ------------------------------------------------------------ parent jump ---

// Deep in a thread the reply a comment answers can be far off screen. Each
// non-root row gets a link back to it. Flattened tails have no DOM parent to
// read (they render as siblings under the depth-capped ancestor), so their
// parent is the nearest preceding flat row exactly one level shallower.
function parentComment(comment) {
    if (comment.classList.contains('comment--flat')) {
        const depth = Number(comment.dataset.depth || 0);
        let sib = comment.previousElementSibling;
        while (sib) {
            if (sib.classList.contains('comment') && Number(sib.dataset.depth || 0) === depth - 1) {
                return sib;
            }
            sib = sib.previousElementSibling;
        }
    }
    return comment.parentElement ? comment.parentElement.closest('.comment') : null;
}

function wireParentLinks() {
    for (const comment of document.querySelectorAll('.comment[data-comment-id]')) {
        const link = comment.querySelector(':scope > .comment-main > .comment-actions > .comment-parent-link');
        if (!link) continue;
        const parent = parentComment(comment);
        if (!parent) continue;
        link.href = `#comment-${parent.dataset.commentId}`;
        link.hidden = false;
    }
}

// ------------------------------------------------------------------ wiring --

let liveRegion = null;
function announce(message) {
    if (!liveRegion) {
        liveRegion = document.createElement('span');
        liveRegion.className = 'visually-hidden';
        liveRegion.setAttribute('aria-live', 'polite');
        document.body.appendChild(liveRegion);
    }
    liveRegion.textContent = message;
}

function copyLink(btn, url, message) {
    const title = btn.getAttribute('title');
    const done = () => {
        announce(message);
        btn.setAttribute('title', 'Copied!');
        btn.classList.add('is-copied');
        clearTimeout(btn._copiedTimer);
        btn._copiedTimer = setTimeout(() => {
            btn.setAttribute('title', title);
            btn.classList.remove('is-copied');
        }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(done, () => announce('Could not copy link'));
    } else {
        // Fallback for non-secure contexts: legacy execCommand path.
        const scratch = document.createElement('textarea');
        scratch.value = url;
        scratch.setAttribute('readonly', '');
        scratch.className = 'visually-hidden';
        document.body.appendChild(scratch);
        scratch.select();
        try {
            document.execCommand('copy');
            done();
        } catch {
            announce('Could not copy link');
        }
        scratch.remove();
    }
}

document.addEventListener('click', (event) => {
    // Sort switcher: reorder in place instead of following the link.
    const sortLink = event.target.closest('[data-comment-sort] .sort-bar__link');
    if (sortLink && SORTS.includes(sortLink.dataset.sort)) {
        event.preventDefault();
        applySort(sortLink.dataset.sort, sortLink.closest('[data-comment-sort]'));
        return;
    }

    const collapseAll = event.target.closest('[data-collapse-all]');
    if (collapseAll) {
        setCollapseAll(collapseAll.getAttribute('aria-pressed') !== 'true');
        syncCollapseAll(collapseAll);
        return;
    }

    // Thread rail: the canonical collapse control (also the keyboard path —
    // native Enter/Space on the focused button lands here).
    const rail = event.target.closest('.comment-collapse');
    if (rail) {
        toggleComment(rail);
        return;
    }

    const permalink = event.target.closest('.comment-permalink');
    if (permalink) {
        copyLink(
            permalink,
            `${location.origin}${location.pathname}${location.search}#comment-${permalink.dataset.commentId}`,
            'Comment link copied to clipboard',
        );
        return;
    }

    const postLink = event.target.closest('[data-copy-link]');
    if (postLink) {
        copyLink(
            postLink,
            `${location.origin}${location.pathname}${location.search}`,
            'Post link copied to clipboard',
        );
        return;
    }

    // Jump to the reply this one answers. Going through the hash reuses the
    // deep-link path, so the parent gets expanded and flashed like any
    // permalink; a same-hash click fires no hashchange, hence the direct call.
    const parentJump = event.target.closest('.comment-parent-link');
    if (parentJump) {
        event.preventDefault();
        const hash = parentJump.getAttribute('href');
        if (location.hash === hash) handleHash();
        else location.hash = hash;
        return;
    }
    // Whole-header toggle: any click on the header that is not an explicit
    // control (user/profile links, chips, permalink, pill) collapses. Leaf
    // comments collapse too — the action mirrors the chevron button exactly.
    const header = event.target.closest('.comment-header');
    if (header) {
        const comment = header.closest('.comment');
        const btn = comment && collapseButton(comment);
        if (btn && !event.target.closest('a, button, input, textarea, select, label')) {
            event.preventDefault();
            toggleComment(btn);
            return;
        }
    }
    const pill = event.target.closest('.comment-replies-pill');
    if (pill) {
        const comment = pill.closest('.comment');
        const btn = comment && collapseButton(comment);
        if (btn) toggleComment(btn);
        return;
    }
    const collapseLink = event.target.closest('.comment-collapse-link');
    if (collapseLink) {
        const comment = collapseLink.closest('.comment');
        const btn = comment && collapseButton(comment);
        if (btn) toggleComment(btn);
    }
});

// Keyboard operability lives entirely on the rail button, which is a real
// <button> with a state-tracking aria-label. The header row is a pointer
// convenience only — it is deliberately NOT focusable, since a second tab
// stop with the identical action on every comment makes a long thread
// exhausting to walk with a keyboard.

// Restore session-collapsed state on load. When no session state exists yet,
// server-set is-collapsed classes stand untouched.
(function init() {
    wireParentLinks();

    // Controls that only work with JS ship hidden and are revealed here.
    const collapseAll = document.querySelector('[data-collapse-all]');
    if (collapseAll) {
        collapseAll.hidden = false;
        syncCollapseAll(collapseAll);
    }

    const sortBar = document.querySelector('[data-comment-sort]');
    if (sortBar) revealActiveSort(sortBar);

    const stored = loadCollapsed();
    if (!stored) return;

    for (const comment of document.querySelectorAll('.comment[data-comment-id]')) {
        const wantCollapsed = stored.has(comment.dataset.commentId);
        if (comment.classList.contains('comment--flat')) {
            if (wantCollapsed && !comment.classList.contains('is-collapsed')) toggleFlat(comment);
            else if (!wantCollapsed && comment.classList.contains('is-collapsed')) expandFlatChain(comment);
        } else if (wantCollapsed !== comment.classList.contains('is-collapsed')) {
            toggleNested(comment);
        }
    }

    // Refresh every visible "N hidden" summary against restored reality.
    document.querySelectorAll('.comment.is-collapsed[data-descendants]').forEach((comment) => {
        const n = Number(comment.dataset.descendants || 0);
        if (comment.classList.contains('comment--flat')) {
            const hidden = [...flatHiddenSiblings(comment)].filter((sib) => sib.classList.contains('flat-hidden')).length;
            updatePill(comment, hidden);
        } else {
            updatePill(comment, n);
        }
    });

    refreshCollapseAll();
})();

handleHash();
