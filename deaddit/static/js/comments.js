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
        link.textContent = expanded
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
        countEl.textContent = `[+] ${hiddenCount} hidden`;
    }
    if (namesEl && hiddenCount != null && hiddenCount > 0) {
        // Recompute from live DOM: children already expanded by the user drop
        // out of the summary; deeper generations are covered by the count.
        const all = directChildNames(comment);
        const names = all.slice(0, NAME_LIMIT);
        namesEl.textContent = names.length ? ` — ${names.join(', ')}${all.length > NAME_LIMIT ? '…' : ''}` : '';
    }
}

// ------------------------------------------------------------- toggling ----

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
    }
    snapshotCollapsed();
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

function copyCommentLink(btn) {
    const url = `${location.origin}${location.pathname}${location.search}#comment-${btn.dataset.commentId}`;
    const title = btn.getAttribute('title');
    const done = () => {
        announce('Comment link copied to clipboard');
        btn.setAttribute('title', 'Copied!');
        clearTimeout(btn._copiedTimer);
        btn._copiedTimer = setTimeout(() => btn.setAttribute('title', title), 1500);
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
    // Chevron/thread-line button: the canonical collapse control (also the
    // keyboard path — native Enter/Space on the focused button lands here).
    const rail = event.target.closest('.comment-collapse');
    if (rail) {
        toggleComment(rail);
        return;
    }

    const permalink = event.target.closest('.comment-permalink');
    if (permalink) {
        copyCommentLink(permalink);
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

// Keyboard operability: Enter/Space on the focused header row toggles, unless
// an inner interactive element has focus. The rail button stays the canonical
// accessible control; this only adds a shortcut for pointerless users.
document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const header = event.target.closest?.('.comment-header');
    if (!header || event.target !== header) return;
    const comment = header.closest('.comment');
    const btn = comment && collapseButton(comment);
    if (!btn) return;
    event.preventDefault();
    toggleComment(btn);
});

// Restore session-collapsed state on load. When no session state exists yet,
// server-set is-collapsed classes stand untouched.
(function init() {
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

    // Refresh every visible "[+] N hidden" summary against restored reality.
    document.querySelectorAll('.comment.is-collapsed[data-descendants]').forEach((comment) => {
        const n = Number(comment.dataset.descendants || 0);
        if (comment.classList.contains('comment--flat')) {
            const hidden = [...flatHiddenSiblings(comment)].filter((sib) => sib.classList.contains('flat-hidden')).length;
            updatePill(comment, hidden);
        } else {
            updatePill(comment, n);
        }
    });
})();

handleHash();
