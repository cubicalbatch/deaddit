// ============================================================
// Admin Content Management
//
//   - Core:        state, list loading, pagination, selection,
//                  bulk delete, alerts
//   - Selection:   the header checkbox selects the current page and
//                  reveals a banner offering "select all pages"; in
//                  that mode bulk delete posts {all: true, search,
//                  subdeaddit} and the server resolves the full
//                  filtered set (see admin.py bulk-delete endpoints).
//
// Every edit flow fetches exactly one row through the targeted
// single-item GET endpoints:
//   GET /admin/api/users/<username>
//   GET /admin/api/subdeaddits/<name>
//   GET /admin/api/posts/<id>
//   GET /admin/api/comments/<id>
// No client-side "fetch everything and search" anywhere.
// The posts subdeaddit filter is rendered server-side by the
// /admin/content route.
// ============================================================

class ContentManager {
    // ---------------- Core ----------------
    constructor() {
        this.currentTab = 'users';
        this.currentPage = 1;
        this.searchTerm = '';
        this.perPage = 25;
        this.selectedItems = new Set();
        // Select-all-pages mode: bulk actions apply to every item matching
        // the current search/filter, not just this page's selection.
        this.selectAllPages = false;
        this.currentTotals = {};
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.loadContent('users');
    }

    capitalize(str) {
        return str.charAt(0).toUpperCase() + str.slice(1);
    }

    setupEventListeners() {
        // Tab switching
        document.querySelectorAll('[data-bs-toggle="tab"]').forEach(tab => {
            tab.addEventListener('shown.bs.tab', (e) => {
                const tabId = e.target.getAttribute('data-bs-target').substring(1);
                this.switchTab(tabId);
            });
        });

        // Search inputs
        ['users', 'subdeaddits', 'posts', 'comments'].forEach(type => {
            const searchInput = document.getElementById(`${type}Search`);
            if (searchInput) {
                searchInput.addEventListener('input', (e) => {
                    this.searchTerm = e.target.value;
                    this.resetSelection();
                    this.loadContent(type);
                });
            }
        });

        // Select all checkboxes
        ['users', 'subdeaddits', 'posts', 'comments'].forEach(type => {
            const selectAllCheckbox = document.getElementById(`selectAll${this.capitalize(type)}`);
            if (selectAllCheckbox) {
                selectAllCheckbox.addEventListener('change', (e) => {
                    this.selectAll(type, e.target.checked);
                });
            }
        });

        // Bulk delete buttons
        ['users', 'subdeaddits', 'posts', 'comments'].forEach(type => {
            const deleteButton = document.getElementById(`deleteSelected${this.capitalize(type)}`);
            if (deleteButton) {
                deleteButton.addEventListener('click', () => {
                    this.bulkDelete(type);
                });
            }
        });

        // Posts subdeaddit filter (options rendered server-side)
        const postsFilter = document.getElementById('postsSubdeadditFilter');
        if (postsFilter) {
            postsFilter.addEventListener('change', () => {
                this.resetSelection();
                this.loadContent('posts');
            });
        }

        // Modal save buttons
        document.getElementById('saveUserChanges')?.addEventListener('click', () => this.saveUser());
        document.getElementById('saveSubdeadditChanges')?.addEventListener('click', () => this.saveSubdeaddit());
        document.getElementById('savePostChanges')?.addEventListener('click', () => this.savePost());
        document.getElementById('saveCommentChanges')?.addEventListener('click', () => this.saveComment());

        // Delete confirmation
        document.getElementById('confirmDeleteBtn')?.addEventListener('click', () => this.executeDelete());
    }

    switchTab(tabId) {
        // Clear the outgoing tab's header checkbox/banner before switching.
        this.resetSelection();
        this.currentTab = tabId;
        this.currentPage = 1;
        this.searchTerm = '';

        // Clear search
        const searchInput = document.getElementById(`${tabId}Search`);
        if (searchInput) searchInput.value = '';

        this.loadContent(tabId);
    }

    async loadContent(type) {
        const url = new URL(`/admin/api/${type}`, window.location.origin);
        url.searchParams.set('page', this.currentPage);
        url.searchParams.set('per_page', this.perPage);

        if (this.searchTerm) {
            url.searchParams.set('search', this.searchTerm);
        }

        if (type === 'posts') {
            const subdeadditFilter = document.getElementById('postsSubdeadditFilter')?.value;
            if (subdeadditFilter) {
                url.searchParams.set('subdeaddit', subdeadditFilter);
            }
        }

        try {
            const response = await fetch(url);
            const data = await response.json();

            if (type === 'users') {
                this.renderUsers(data);
            } else if (type === 'subdeaddits') {
                this.renderSubdeaddits(data);
            } else if (type === 'posts') {
                this.renderPosts(data);
            } else if (type === 'comments') {
                this.renderComments(data);
            }

            this.renderPagination(type, data);
            this.currentTotals[type] = data.total || 0;
            this.renderSelectAllBanner(type);
        } catch (error) {
            console.error('Error loading content:', error);
            this.showAlert('Error loading content', 'danger');
        }
    }

    renderPagination(type, data) {
        const pagination = document.getElementById(`${type}Pagination`);
        pagination.replaceChildren();

        if (data.pages <= 1) return;

        const addPageLink = (label, page) => {
            const li = document.createElement('li');
            li.className = 'page-item';
            const link = document.createElement('a');
            link.className = 'page-link';
            link.href = '#';
            link.textContent = label;
            link.addEventListener('click', (e) => {
                e.preventDefault();
                this.goToPage(page);
            });
            li.appendChild(link);
            pagination.appendChild(li);
        };

        if (data.current_page > 1) {
            addPageLink('Previous', data.current_page - 1);
        }

        const startPage = Math.max(1, data.current_page - 2);
        const endPage = Math.min(data.pages, data.current_page + 2);

        for (let i = startPage; i <= endPage; i++) {
            const li = document.createElement('li');
            li.className = `page-item ${i === data.current_page ? 'active' : ''}`;
            const link = document.createElement('a');
            link.className = 'page-link';
            link.href = '#';
            link.textContent = String(i);
            link.addEventListener('click', (e) => {
                e.preventDefault();
                this.goToPage(i);
            });
            li.appendChild(link);
            pagination.appendChild(li);
        }

        if (data.current_page < data.pages) {
            addPageLink('Next', data.current_page + 1);
        }
    }

    goToPage(page) {
        this.currentPage = page;
        this.resetSelection();
        this.loadContent(this.currentTab);
    }

    // Row checkboxes are always addressed through the active tab's table:
    // hidden panes keep their old rows in the DOM, and a global
    // `.item-checkbox` query would contaminate the selection with them.
    tableCheckboxes(type = this.currentTab) {
        return document.querySelectorAll(`#${type}Table .item-checkbox`);
    }

    setupItemCheckboxes() {
        this.tableCheckboxes().forEach(checkbox => {
            checkbox.addEventListener('change', (e) => {
                const id = e.target.getAttribute('data-id');
                if (e.target.checked) {
                    this.selectedItems.add(id);
                } else {
                    this.selectedItems.delete(id);
                    // Uncheck select all if any item is unchecked; a
                    // partial selection always drops all-pages mode.
                    const selectAllCheckbox = document.getElementById(`selectAll${this.capitalize(this.currentTab)}`);
                    if (selectAllCheckbox) selectAllCheckbox.checked = false;
                    if (this.selectAllPages) {
                        this.selectAllPages = false;
                        this.renderSelectAllBanner();
                    }
                }
            });
        });
    }

    selectAll(type, checked) {
        this.tableCheckboxes(type).forEach(checkbox => {
            checkbox.checked = checked;
            const id = checkbox.getAttribute('data-id');
            if (checked) {
                this.selectedItems.add(id);
            } else {
                this.selectedItems.delete(id);
            }
        });
        if (!checked) this.selectAllPages = false;
        this.renderSelectAllBanner(type);
    }

    // Clear every trace of a selection: row checkboxes, the header
    // checkbox, all-pages mode, and the banner for the active tab.
    resetSelection() {
        this.selectedItems.clear();
        this.selectAllPages = false;
        const selectAllCheckbox = document.getElementById(`selectAll${this.capitalize(this.currentTab)}`);
        if (selectAllCheckbox) selectAllCheckbox.checked = false;
        this.tableCheckboxes().forEach(checkbox => {
            checkbox.checked = false;
        });
        this.renderSelectAllBanner();
    }

    // Banner under the table controls. Hidden unless the header checkbox
    // selected the page; then it offers "select all pages" and, once
    // active, a way back out.
    renderSelectAllBanner(type = this.currentTab) {
        const banner = document.getElementById(`${type}SelectAllBanner`);
        if (!banner) return;

        const total = this.currentTotals[type] || 0;
        const header = document.getElementById(`selectAll${this.capitalize(type)}`);
        const headerChecked = header ? header.checked : false;

        if (!headerChecked || total === 0 || this.currentTab !== type) {
            banner.style.display = 'none';
            return;
        }

        banner.style.display = 'flex';
        banner.replaceChildren();
        const span = document.createElement('span');
        const strong = document.createElement('strong');
        strong.textContent = total.toLocaleString();
        span.append('All ', strong, ` ${type}`);
        if (this.selectAllPages && this.searchTerm) span.append(' matching the current search');
        span.append(this.selectAllPages ? ' are selected (every page).' : ' on this page are selected.');
        banner.appendChild(span);

        const link = document.createElement('a');
        link.href = '#';
        link.className = this.selectAllPages ? 'banner-clear' : 'banner-all-pages';
        link.textContent = this.selectAllPages
            ? 'Clear selection'
            : `Select all ${total.toLocaleString()} ${type} across all pages`;
        link.addEventListener('click', (e) => {
            e.preventDefault();
            if (this.selectAllPages) {
                this.resetSelection();
            } else {
                this.selectAllPages = true;
                this.renderSelectAllBanner(type);
            }
        });
        banner.appendChild(link);
    }

    truncate(text, length) {
        if (!text) return '';
        const value = String(text);
        return value.length > length ? value.substring(0, length) + '...' : value;
    }

    createTextCell(row, value, className = '') {
        const cell = document.createElement('td');
        if (className) cell.className = className;
        cell.textContent = value == null ? '' : String(value);
        row.appendChild(cell);
        return cell;
    }

    createCheckboxCell(row, id, label) {
        const cell = document.createElement('td');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'item-checkbox';
        checkbox.dataset.id = String(id);
        checkbox.setAttribute('aria-label', label);
        cell.appendChild(checkbox);
        row.appendChild(cell);
    }

    createActionCell(row, editCall, deleteCall, viewAttrs = null, promptCall = null) {
        const cell = document.createElement('td');
        cell.appendChild(this.actionButtons(editCall, deleteCall, viewAttrs, promptCall));
        row.appendChild(cell);
    }

    actionButtons(editCall, deleteCall, viewAttrs = null, promptCall = null) {
        const container = document.createElement('div');
        container.className = 'action-buttons';
        const makeButton = (className, title, iconClass, label, handler, responsiveClass = '') => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = `btn btn-sm ${className}`;
            button.title = title;
            button.addEventListener('click', handler);
            const icon = document.createElement('i');
            icon.className = iconClass;
            button.appendChild(icon);
            const text = document.createElement('span');
            text.className = `${responsiveClass} ms-1`.trim();
            text.textContent = label;
            button.appendChild(text);
            return button;
        };

        container.appendChild(makeButton('btn-primary', 'Edit', 'bi bi-pencil', 'Edit', editCall, 'd-none d-sm-inline'));
        container.appendChild(makeButton('btn-danger', 'Delete', 'bi bi-trash', 'Delete', deleteCall, 'd-none d-sm-inline'));
        if (viewAttrs) {
            const view = document.createElement('a');
            view.href = viewAttrs.href;
            view.className = 'btn btn-sm btn-info';
            view.target = '_blank';
            view.rel = 'noopener';
            view.title = viewAttrs.title;
            const icon = document.createElement('i');
            icon.className = 'bi bi-eye';
            view.appendChild(icon);
            const text = document.createElement('span');
            text.className = 'd-none d-sm-inline ms-1';
            text.textContent = 'View';
            view.appendChild(text);
            container.appendChild(view);
        }
        if (promptCall) {
            container.appendChild(makeButton('btn-secondary', 'Originating prompt', 'bi bi-magic', 'Prompt', promptCall, 'd-none d-lg-inline'));
        }
        return container;
    }

    // ---------------- Users ----------------
    renderUsers(data) {
        const tbody = document.querySelector('#usersTable tbody');
        tbody.replaceChildren();

        data.users.forEach(user => {
            const row = document.createElement('tr');
            this.createCheckboxCell(row, user.username, `Select user ${user.username}`);
            const usernameCell = this.createTextCell(row, user.username);
            if (user.is_troll) {
                const badge = document.createElement('span');
                badge.className = 'badge bg-danger';
                badge.textContent = 'troll';
                usernameCell.append(' ', badge);
            }
            this.createTextCell(row, user.age, 'd-none d-md-table-cell');
            this.createTextCell(row, user.gender, 'd-none d-lg-table-cell');
            this.createTextCell(row, user.occupation, 'd-none d-lg-table-cell');
            this.createTextCell(row, user.posts_count, 'd-none d-sm-table-cell');
            this.createTextCell(row, user.comments_count, 'd-none d-sm-table-cell');
            this.createActionCell(
                row,
                () => this.editUser(user.username),
                () => this.deleteUser(user.username)
            );
            tbody.appendChild(row);
        });

        this.setupItemCheckboxes();
    }
    async editUser(username) {
        try {
            // One targeted call for exactly this row.
            const response = await fetch(`/admin/api/users/${encodeURIComponent(username)}`);
            if (!response.ok) return;
            const user = await response.json();

            // Populate form
            document.getElementById('editUserId').value = username;
            document.getElementById('editUserUsername').value = user.username;
            document.getElementById('editUserAge').value = user.age || '';
            document.getElementById('editUserGender').value = user.gender || '';
            document.getElementById('editUserOccupation').value = user.occupation || '';
            document.getElementById('editUserEducation').value = user.education || '';
            document.getElementById('editUserBio').value = user.bio || '';
            document.getElementById('editUserInterests').value = user.interests || '';
            document.getElementById('editUserWritingStyle').value = user.writing_style || '';
            const subs = Array.isArray(user.subscriptions) ? user.subscriptions.join(', ') : (user.subscriptions || '');
            document.getElementById('editUserSubscriptions').value = subs;
            const caps = user.rate_caps || {};
            document.getElementById('editUserRatePost').value = caps.post ?? '';
            document.getElementById('editUserRateComment').value = caps.comment ?? '';
            document.getElementById('editUserRateVote').value = caps.vote ?? '';
            document.getElementById('editUserTroll').checked = Boolean(user.is_troll);
            // Show modal
            new bootstrap.Modal(document.getElementById('editUserModal')).show();
        } catch (error) {
            console.error('Error loading user:', error);
            this.showAlert('Error loading user data', 'danger');
        }
    }

    async saveUser() {
        const username = document.getElementById('editUserId').value;
        const data = {
            age: parseInt(document.getElementById('editUserAge').value) || null,
            gender: document.getElementById('editUserGender').value,
            occupation: document.getElementById('editUserOccupation').value,
            education: document.getElementById('editUserEducation').value,
            bio: document.getElementById('editUserBio').value,
            interests: document.getElementById('editUserInterests').value,
            personality_traits: document.getElementById('editUserPersonality').value,
            writing_style: document.getElementById('editUserWritingStyle').value,
            subscriptions: document.getElementById('editUserSubscriptions').value,
            is_troll: document.getElementById('editUserTroll').checked,
            rate_caps: {
                post: this.capInput('editUserRatePost'),
                comment: this.capInput('editUserRateComment'),
                vote: this.capInput('editUserRateVote')
            }
        };

        try {
            const response = await fetch(`/admin/api/users/${encodeURIComponent(username)}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();
            if (result.success) {
                bootstrap.Modal.getInstance(document.getElementById('editUserModal')).hide();
                this.loadContent('users');
                this.showAlert('User updated successfully', 'success');
            } else {
                this.showAlert('Error updating user: ' + result.error, 'danger');
            }
        } catch (error) {
            console.error('Error saving user:', error);
            this.showAlert('Error saving user', 'danger');
        }
    }

    capInput(id) {
        const raw = document.getElementById(id).value.trim();
        if (raw === '') return null;
        const parsed = parseInt(raw, 10);
        return (Number.isNaN(parsed) || parsed < 0) ? null : parsed;
    }

    deleteUser(username) {
        this.showDeleteConfirmation('user', username, `Are you sure you want to delete user "${username}"?`);
    }

    // ---------------- Subdeaddits ----------------
    renderSubdeaddits(data) {
        const tbody = document.querySelector('#subdeadditsTable tbody');
        tbody.replaceChildren();

        data.subdeaddits.forEach(sub => {
            const row = document.createElement('tr');
            this.createCheckboxCell(row, sub.name, `Select subdeaddit ${sub.name}`);
            this.createTextCell(row, sub.name);
            this.createTextCell(row, this.truncate(sub.description, 100), 'd-none d-md-table-cell');
            this.createTextCell(row, sub.posts_count, 'd-none d-sm-table-cell');
            this.createTextCell(row, '-', 'd-none d-lg-table-cell');
            this.createActionCell(
                row,
                () => this.editSubdeaddit(sub.name),
                () => this.deleteSubdeaddit(sub.name)
            );
            tbody.appendChild(row);
        });

        this.setupItemCheckboxes();
    }

    async editSubdeaddit(name) {
        try {
            // One targeted call for exactly this row.
            const response = await fetch(`/admin/api/subdeaddits/${encodeURIComponent(name)}`);
            if (!response.ok) return;
            const sub = await response.json();

            // Populate form
            document.getElementById('editSubdeadditId').value = name;
            document.getElementById('editSubdeadditName').value = sub.name;
            document.getElementById('editSubdeadditDescription').value = sub.description || '';
            document.getElementById('editSubdeadditPostTypes').value = sub.post_types || '';

            // Show modal
            new bootstrap.Modal(document.getElementById('editSubdeadditModal')).show();
        } catch (error) {
            console.error('Error loading subdeaddit:', error);
            this.showAlert('Error loading subdeaddit data', 'danger');
        }
    }

    async saveSubdeaddit() {
        const name = document.getElementById('editSubdeadditId').value;
        const data = {
            description: document.getElementById('editSubdeadditDescription').value,
            post_types: document.getElementById('editSubdeadditPostTypes').value
        };

        try {
            const response = await fetch(`/admin/api/subdeaddits/${encodeURIComponent(name)}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();
            if (result.success) {
                bootstrap.Modal.getInstance(document.getElementById('editSubdeadditModal')).hide();
                this.loadContent('subdeaddits');
                this.showAlert('Subdeaddit updated successfully', 'success');
            } else {
                this.showAlert('Error updating subdeaddit: ' + result.error, 'danger');
            }
        } catch (error) {
            console.error('Error saving subdeaddit:', error);
            this.showAlert('Error saving subdeaddit', 'danger');
        }
    }

    deleteSubdeaddit(name) {
        this.showDeleteConfirmation('subdeaddit', name, `Are you sure you want to delete subdeaddit "${name}"?`);
    }

    // ---------------- Posts ----------------
    renderPosts(data) {
        const tbody = document.querySelector('#postsTable tbody');
        tbody.replaceChildren();

        data.posts.forEach(post => {
            const row = document.createElement('tr');
            const createdDate = new Date(post.created_at).toLocaleDateString();
            this.createCheckboxCell(row, post.id, `Select post ${post.id}`);
            this.createTextCell(row, this.truncate(post.title, 50));
            this.createTextCell(row, post.username, 'd-none d-sm-table-cell');
            this.createTextCell(row, post.subdeaddit_name, 'd-none d-md-table-cell');
            this.createTextCell(row, post.score, 'd-none d-sm-table-cell');
            this.createTextCell(row, post.comments_count, 'd-none d-lg-table-cell');
            this.createTextCell(row, createdDate, 'd-none d-lg-table-cell');
            this.createActionCell(
                row,
                () => this.editPost(post.id),
                () => this.deletePost(post.id),
                {href: `/d/${encodeURIComponent(post.subdeaddit_name)}/${encodeURIComponent(post.id)}`, title: 'View'},
                () => this.viewPostPrompt(post.id)
            );
            tbody.appendChild(row);
        });

        this.setupItemCheckboxes();
    }
    async editPost(id) {
        try {
            // One targeted call for exactly this row.
            const response = await fetch(`/admin/api/posts/${encodeURIComponent(id)}`);
            if (!response.ok) return;
            const post = await response.json();

            // Populate form
            document.getElementById('editPostId').value = id;
            document.getElementById('editPostTitle').value = post.title;
            document.getElementById('editPostContent').value = post.content;
            document.getElementById('editPostUpvotes').value = post.score;
            document.getElementById('editPostType').value = post.post_type || '';

            // Show modal
            new bootstrap.Modal(document.getElementById('editPostModal')).show();
        } catch (error) {
            console.error('Error loading post:', error);
            this.showAlert('Error loading post data', 'danger');
        }
    }

    async savePost() {
        const id = document.getElementById('editPostId').value;
        const data = {
            title: document.getElementById('editPostTitle').value,
            content: document.getElementById('editPostContent').value,
            score: parseInt(document.getElementById('editPostUpvotes').value) || 0,
            post_type: document.getElementById('editPostType').value
        };

        try {
            const response = await fetch(`/admin/api/posts/${encodeURIComponent(id)}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();
            if (result.success) {
                bootstrap.Modal.getInstance(document.getElementById('editPostModal')).hide();
                this.loadContent('posts');
                this.showAlert('Post updated successfully', 'success');
            } else {
                this.showAlert('Error updating post: ' + result.error, 'danger');
            }
        } catch (error) {
            console.error('Error saving post:', error);
            this.showAlert('Error saving post', 'danger');
        }
    }

    deletePost(id) {
        this.showDeleteConfirmation('post', id, `Are you sure you want to delete this post?`);
    }

    // ---------------- Comments ----------------
    renderComments(data) {
        const tbody = document.querySelector('#commentsTable tbody');
        tbody.replaceChildren();

        data.comments.forEach(comment => {
            const row = document.createElement('tr');
            const createdDate = new Date(comment.created_at).toLocaleDateString();
            this.createCheckboxCell(row, comment.id, `Select comment ${comment.id}`);
            this.createTextCell(row, this.truncate(comment.content, 80));
            this.createTextCell(row, comment.username, 'd-none d-sm-table-cell');
            this.createTextCell(row, this.truncate(comment.post_title, 30), 'd-none d-md-table-cell');
            this.createTextCell(row, comment.parent_id ? 'Reply' : 'Root', 'd-none d-lg-table-cell');
            this.createTextCell(row, comment.score, 'd-none d-sm-table-cell');
            this.createTextCell(row, createdDate, 'd-none d-lg-table-cell');
            this.createActionCell(
                row,
                () => this.editComment(comment.id),
                () => this.deleteComment(comment.id),
                {href: `/d/${encodeURIComponent(comment.subdeaddit_name)}/${encodeURIComponent(comment.post_id)}`, title: 'View Post'},
                () => this.viewCommentPrompt(comment.id)
            );
            tbody.appendChild(row);
        });

        this.setupItemCheckboxes();
    }

    async editComment(id) {
        try {
            // One targeted call for exactly this row.
            const response = await fetch(`/admin/api/comments/${encodeURIComponent(id)}`);
            if (!response.ok) return;
            const comment = await response.json();

            // Populate form
            document.getElementById('editCommentId').value = id;
            document.getElementById('editCommentContent').value = comment.content;
            document.getElementById('editCommentUpvotes').value = comment.score;

            // Show modal
            new bootstrap.Modal(document.getElementById('editCommentModal')).show();
        } catch (error) {
            console.error('Error loading comment:', error);
            this.showAlert('Error loading comment data', 'danger');
        }
    }

    async saveComment() {
        const id = document.getElementById('editCommentId').value;
        const data = {
            content: document.getElementById('editCommentContent').value,
            score: parseInt(document.getElementById('editCommentUpvotes').value) || 0
        };

        try {
            const response = await fetch(`/admin/api/comments/${encodeURIComponent(id)}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();
            if (result.success) {
                bootstrap.Modal.getInstance(document.getElementById('editCommentModal')).hide();
                this.loadContent('comments');
                this.showAlert('Comment updated successfully', 'success');
            } else {
                this.showAlert('Error updating comment: ' + result.error, 'danger');
            }
        } catch (error) {
            console.error('Error saving comment:', error);
            this.showAlert('Error saving comment', 'danger');
        }
    }

    deleteComment(id) {
        this.showDeleteConfirmation('comment', id, `Are you sure you want to delete this comment?`);
    }

    // ---------------- Originating prompt ----------------
    viewPostPrompt(id) { this.viewContentPrompt('posts', id); }

    viewCommentPrompt(id) { this.viewContentPrompt('comments', id); }

    async viewContentPrompt(kind, id) {
        const body = document.getElementById('viewPromptBody');
        const loading = document.createElement('div');
        loading.className = 'text-muted';
        loading.textContent = 'Loading…';
        body.replaceChildren(loading);
        new bootstrap.Modal(document.getElementById('viewPromptModal')).show();
        try {
            const response = await fetch(`/admin/api/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/prompt`);
            const data = await response.json();
            this.renderPromptOrigin(body, data);
        } catch (error) {
            console.error('Error loading prompt:', error);
            const failure = document.createElement('div');
            failure.className = 'text-danger';
            failure.textContent = 'Error loading prompt.';
            body.replaceChildren(failure);
        }
    }

    renderPromptOrigin(body, data) {
        body.replaceChildren();
        if (!data.found) {
            body.textContent = 'No originating agent prompt — this content was seeded or created outside the agent runtime.';
            return;
        }
        const meta = document.createElement('div');
        meta.className = 'small text-muted mb-2';
        const bits = [`tool: ${data.tool_call.name}`];
        if (data.run) bits.push(`run #${data.run.id} by ${data.run.persona} (${data.run.trigger})`);
        if (data.turn && data.turn.model) bits.push(`model: ${data.turn.model}`);
        meta.textContent = bits.join(' · ');
        body.appendChild(meta);

        if (!data.turn) {
            const p = document.createElement('div');
            p.className = 'text-muted';
            p.textContent = 'The tool call exists but its LLM turn was not retained.';
            body.appendChild(p);
            return;
        }
        body.appendChild(this.promptHeading('Prompt sent to the LLM (verbatim)'));
        (data.turn.request_messages || []).forEach(m => body.appendChild(this.promptMessageBlock(m)));
        body.appendChild(this.promptHeading('LLM response (verbatim)'));
        body.appendChild(this.promptMessageBlock(data.turn.response_message));
    }

    promptHeading(text) {
        const h = document.createElement('h6');
        h.className = 'mt-3 mb-2';
        h.textContent = text;
        return h;
    }

    promptMessageBlock(message) {
        const wrap = document.createElement('div');
        wrap.className = 'border rounded p-2 mb-2';
        const role = (message && message.role) || '?';
        const badge = document.createElement('span');
        badge.className = 'badge bg-info mb-1';
        badge.textContent = role;
        wrap.appendChild(badge);
        const content = message && message.content !== undefined ? message.content : message;
        const pre = document.createElement('pre');
        pre.className = 'small mb-0';
        pre.style.whiteSpace = 'pre-wrap';
        pre.textContent = typeof content === 'string' ? content : JSON.stringify(content, null, 2);
        wrap.appendChild(pre);
        return wrap;
    }

    // ---------------- Deletion (shared) ----------------
    bulkDelete(type) {
        // All-pages mode: the server resolves every item matching the
        // current search/filter, so no id list is sent.
        if (this.selectAllPages && type === this.currentTab) {
            const total = (this.currentTotals[type] || 0).toLocaleString();
            const message = `Are you sure you want to delete ALL ${total} ${type} across every page?`;
            const impacts = {
                users: 'This will permanently delete every matching user, plus ALL their posts, comments, votes, and agent history.',
                subdeaddits: 'This will permanently delete every matching subdeaddit, plus ALL their posts and comments.',
                posts: 'This will permanently delete every matching post, plus ALL its comments, images, and websites.',
                comments: 'This will permanently delete every matching comment, plus ALL replies to them.'
            };
            this.showDeleteConfirmation(`bulk-all-${type}`, null, message, impacts[type]);
            return;
        }

        if (this.selectedItems.size === 0) {
            this.showAlert('No items selected', 'warning');
            return;
        }

        const count = this.selectedItems.size;
        const message = `Are you sure you want to delete ${count} ${type}?`;
        this.showDeleteConfirmation(`bulk-${type}`, Array.from(this.selectedItems), message);
    }

    showDeleteConfirmation(type, id, message, impactText = null) {
        this.pendingDelete = { type, id };

        document.getElementById('deleteConfirmMessage').textContent = message;

        // Show impact warning for cascading deletes
        const warningDiv = document.getElementById('deleteImpactWarning');
        if (impactText) {
            warningDiv.style.display = 'block';
            document.getElementById('deleteImpactText').textContent = impactText;
        } else if (type === 'user' || type === 'subdeaddit' || type === 'post' || type === 'comment') {
            warningDiv.style.display = 'block';
            if (type === 'user') {
                document.getElementById('deleteImpactText').textContent = 'This will also delete all posts and comments by this user.';
            } else if (type === 'subdeaddit') {
                document.getElementById('deleteImpactText').textContent = 'This will also delete all posts and comments in this subdeaddit.';
            } else if (type === 'post') {
                document.getElementById('deleteImpactText').textContent = 'This will also delete all comments on this post.';
            } else if (type === 'comment') {
                document.getElementById('deleteImpactText').textContent = 'This will also delete all replies to this comment.';
            }
        } else {
            warningDiv.style.display = 'none';
        }

        new bootstrap.Modal(document.getElementById('deleteConfirmModal')).show();
    }

    async executeDelete() {
        const { type, id } = this.pendingDelete;

        try {
            let response;

            if (type.startsWith('bulk-all-')) {
                // Server-side resolution of the full filtered set.
                const bulkType = type.replace('bulk-all-', '');
                const endpoint = `/admin/api/${bulkType}/bulk-delete`;
                const body = {all: true};
                if (this.searchTerm) body.search = this.searchTerm;
                if (bulkType === 'posts') {
                    const subdeadditFilter = document.getElementById('postsSubdeadditFilter')?.value;
                    if (subdeadditFilter) body.subdeaddit = subdeadditFilter;
                }

                response = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
            } else if (type.startsWith('bulk-')) {
                const bulkType = type.replace('bulk-', '');
                const endpoint = `/admin/api/${bulkType}/bulk-delete`;
                const bodyKey = bulkType === 'users' ? 'usernames' :
                               bulkType === 'subdeaddits' ? 'names' :
                               bulkType === 'posts' ? 'post_ids' : 'comment_ids';

                response = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({[bodyKey]: id})
                });
            } else {
                let endpoint;
                if (type === 'user') endpoint = `/admin/api/users/${encodeURIComponent(id)}`;
                else if (type === 'subdeaddit') endpoint = `/admin/api/subdeaddits/${encodeURIComponent(id)}`;
                else if (type === 'post') endpoint = `/admin/api/posts/${encodeURIComponent(id)}`;
                else if (type === 'comment') endpoint = `/admin/api/comments/${encodeURIComponent(id)}`;

                response = await fetch(endpoint, { method: 'DELETE' });
            }

            const result = await response.json();
            if (result.success) {
                bootstrap.Modal.getInstance(document.getElementById('deleteConfirmModal')).hide();
                this.resetSelection();
                this.loadContent(this.currentTab);

                let message = 'Deleted successfully';
                if (result.deleted) {
                    const deleted = result.deleted;
                    message = `Deleted: ${Object.entries(deleted).map(([k,v]) => `${v} ${k}`).join(', ')}`;
                }
                this.showAlert(message, 'success');
            } else {
                this.showAlert('Error deleting: ' + result.error, 'danger');
            }
        } catch (error) {
            console.error('Error deleting:', error);
            this.showAlert('Error deleting content', 'danger');
        }
    }

    showAlert(message, type) {
        const alert = document.createElement('div');
        alert.className = `alert alert-${type} alert-dismissible fade show position-fixed`;
        alert.style.cssText = 'top: 20px; right: 20px; z-index: 9999; min-width: 300px;';
        alert.appendChild(document.createTextNode(String(message ?? '')));
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close';
        close.dataset.bsDismiss = 'alert';
        alert.appendChild(close);

        document.body.appendChild(alert);

        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            if (alert.parentNode) {
                alert.remove();
            }
        }, 5000);
    }
}

// Initialize content manager when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.contentManager = new ContentManager();
});
