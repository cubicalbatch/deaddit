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

function setAria(comment, expanded) {
    const btn = comment.querySelector(':scope > .comment-collapse');
    if (btn) btn.setAttribute('aria-expanded', String(expanded));
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

    // Each flat item renders as a single article, so the hidden-sibling count
    // is exactly the number of newly hidden comments.
    let hiddenCount = 0;
    for (const sib of flatHiddenSiblings(comment)) {
        sib.classList.toggle('flat-hidden', collapsed);
        if (collapsed) hiddenCount += 1;
    }

    const pill = comment.querySelector('.comment-replies-pill');
    if (pill && collapsed && hiddenCount > 0) {
        pill.textContent = `(show ${hiddenCount} ${hiddenCount === 1 ? 'reply' : 'replies'})`;
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

function toggleComment(btn) {
    const comment = btn.closest('.comment');
    if (!comment) return;
    if (comment.classList.contains('comment--flat')) {
        if (comment.classList.contains('is-collapsed')) expandFlatChain(comment);
        else toggleFlat(comment);
    } else {
        toggleNested(comment);
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

// -------------------------------------------------------------- copy link ---

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
    const url = `${location.origin}${location.pathname}#comment-${btn.dataset.commentId}`;
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

// ------------------------------------------------------------------ wiring --

document.addEventListener('click', (event) => {
    const collapseBtn = event.target.closest('.comment-collapse');
    if (collapseBtn) {
        toggleComment(collapseBtn);
        return;
    }
    const pill = event.target.closest('.comment-replies-pill');
    if (pill) {
        const comment = pill.closest('.comment');
        const rail = comment && comment.querySelector(':scope > .comment-collapse');
        if (rail) toggleComment(rail);
        return;
    }
    const permalink = event.target.closest('.comment-permalink');
    if (permalink) copyCommentLink(permalink);
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
})();

handleHash();
