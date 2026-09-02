// Visitor voting: clicking a vote arrow POSTs to /api/vote and re-renders
// the widget's score + active state in place. Clicking the already-active
// arrow clears the vote (value 0), Reddit-style toggle-off.

function currentVote(widget) {
    if (widget.querySelector('.vote-up').classList.contains('is-upvoted')) return 1;
    if (widget.querySelector('.vote-down').classList.contains('is-downvoted')) return -1;
    return 0;
}

function render(widget, myVote, score) {
    const up = widget.querySelector('.vote-up');
    const down = widget.querySelector('.vote-down');
    up.classList.toggle('is-upvoted', myVote === 1);
    down.classList.toggle('is-downvoted', myVote === -1);
    up.setAttribute('aria-pressed', String(myVote === 1));
    down.setAttribute('aria-pressed', String(myVote === -1));
    widget.querySelector('.vote-score').textContent = score;
}

document.addEventListener('click', (ev) => {
    const arrow = ev.target.closest('.vote-arrow');
    const widget = arrow && arrow.closest('.vote-widget');
    if (!widget || !widget.dataset.targetId || arrow.disabled) return;

    const current = currentVote(widget);
    const direction = arrow.classList.contains('vote-up') ? 1 : -1;
    const wanted = current === direction ? 0 : direction; // toggle off on re-click

    fetch('/api/vote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            target: widget.dataset.targetType,
            id: Number(widget.dataset.targetId),
            value: wanted,
        }),
    })
        .then((resp) => Promise.all([resp.ok, resp.json()]))
        .then(([ok, data]) => {
            if (ok && data.status === 'ok') {
                render(widget, data.my_vote, data.score);
            } else if (data && data.reason) {
                // Domain rejection (removed post, downvotes disabled, ...):
                // surface the reason on the widget without changing state.
                widget.title = data.reason;
            }
        })
        .catch(() => {
            widget.title = 'vote failed, try again';
        });
});
