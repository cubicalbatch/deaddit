// Deaddit admin base behavior (vanilla JS, no dependencies).
// Loaded deferred after vendor bundles; all functions stay global
// because child admin templates call them from their own scripts.

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
        localStorage.removeItem('nightMode');
        syncToggle();
    });
}
syncToggle();

// Initialize WebSocket connection for real-time updates
const socket = io('/admin', {
    transports: ['polling'],
    upgrade: false,
    timeout: 20000,
    forceNew: true
});
let isConnected = false;

socket.on('connect', function() {
    console.log('Connected to admin WebSocket');
    isConnected = true;

    // Join job updates room
    socket.emit('join_job_updates', {});
});

socket.on('disconnect', function() {
    console.log('Disconnected from admin WebSocket');
    isConnected = false;
});

socket.on('job_update', function(data) {
    console.log('Received job update:', data);
    updateJobUI(data);
});


function updateJobUI(jobData) {
    const jobId = jobData.job_id;

    // Update status elements
    const statusElements = document.querySelectorAll(`[data-job-id="${jobId}"]`);
    statusElements.forEach(element => {
        element.className = `job-status-${jobData.status}`;
        element.innerHTML = `
            <i class="bi bi-${getStatusIcon(jobData.status)}"></i>
            ${jobData.status.toUpperCase()}
        `;
    });

    // Update progress bars
    const progressBars = document.querySelectorAll(`[data-job-progress="${jobId}"]`);
    progressBars.forEach(progressBar => {
        if (jobData.total_items > 0) {
            const percentage = Math.round((jobData.progress / jobData.total_items) * 100);
            progressBar.style.width = percentage + '%';
            progressBar.textContent = `${jobData.progress}/${jobData.total_items}`;

            // Add animation for running jobs
            if (jobData.status === 'running') {
                progressBar.classList.add('progress-bar-striped', 'progress-bar-animated');
            } else {
                progressBar.classList.remove('progress-bar-striped', 'progress-bar-animated');
            }
        }
    });

    // Update error messages
    if (jobData.error_message) {
        const errorElements = document.querySelectorAll(`[data-job-error="${jobId}"]`);
        errorElements.forEach(element => {
            element.textContent = jobData.error_message;
            element.style.display = 'block';
        });
    }

    // Show completion notifications
    if (jobData.status === 'completed') {
        showJobNotification(`Job ${jobId} completed successfully!`, 'success');
    } else if (jobData.status === 'failed') {
        showJobNotification(`Job ${jobId} failed: ${jobData.error_message}`, 'danger');
    }
}

function getStatusIcon(status) {
    const icons = {
        'pending': 'clock',
        'running': 'play-circle',
        'completed': 'check-circle',
        'failed': 'x-circle',
        'cancelled': 'dash-circle'
    };
    return icons[status] || 'question-circle';
}

function showJobNotification(message, type) {
    // Create notification toast
    const toastContainer = document.getElementById('toast-container') || createToastContainer();
    const toastId = 'toast-' + Date.now();

    toastContainer.insertAdjacentHTML('beforeend', `
        <div id="${toastId}" class="toast align-items-center text-white bg-${type} border-0" role="alert">
            <div class="d-flex">
                <div class="toast-body">
                    ${message}
                </div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
            </div>
        </div>
    `);

    const toast = new bootstrap.Toast(document.getElementById(toastId));
    toast.show();
}

function createToastContainer() {
    const container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
    container.style.setProperty('z-index', 'var(--z-toast)');
    document.body.appendChild(container);
    return container;
}

// Fallback polling for when WebSocket is not connected
function refreshJobStatus() {
    if (isConnected) return; // Skip if WebSocket is working

    const statusElements = document.querySelectorAll('[data-job-id]');
    statusElements.forEach(element => {
        const jobId = element.getAttribute('data-job-id');
        fetch(`/admin/api/jobs/${jobId}/status`)
            .then(response => response.json())
            .then(data => {
                if (data.status) {
                    updateJobUI({
                        job_id: parseInt(jobId),
                        status: data.status,
                        progress: data.progress || 0,
                        total_items: data.total_items || 1,
                        error_message: data.error_message
                    });
                }
            })
            .catch(error => console.error('Error fetching job status:', error));
    });
}

// Fallback refresh every 30 seconds (only when WebSocket is disconnected)
setInterval(refreshJobStatus, 30000);

// Refresh stats on dashboard
function refreshStats() {
    if (window.location.pathname === '/admin/' || window.location.pathname === '/admin/dashboard') {
        fetch('/admin/api/jobs/stats')
            .then(response => response.json())
            .then(data => {
                const updateStat = (id, value) => {
                    const element = document.getElementById(id);
                    if (element) element.textContent = value;
                };

                if (data.database) {
                    updateStat('pending-jobs', data.database.pending);
                    updateStat('running-jobs', data.database.running);
                    updateStat('completed-jobs', data.database.completed);
                    updateStat('failed-jobs', data.database.failed);
                }
            })
            .catch(error => console.error('Error fetching stats:', error));
    }
}

// Refresh stats every 60 seconds
setInterval(refreshStats, 60000);

// Accessible names for icon-only job links (tables render a bare
// eye icon; axe link-name requires an aria-label on those).
(function () {
    document.querySelectorAll('a[href*="/admin/jobs/"]').forEach(function (link) {
        var href = link.getAttribute('href');
        var match = href.match(/\/admin\/jobs\/(\d+)/);
        if (match && !link.textContent.trim() && !link.getAttribute('aria-label')) {
            link.setAttribute('aria-label', 'View job ' + match[1]);
        }
    });
})();
