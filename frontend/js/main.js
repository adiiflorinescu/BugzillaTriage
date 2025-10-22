const sidebarNav = document.getElementById('sidebar-nav');
const notificationsContainer = document.getElementById('notifications');

/**
 * Displays a toast notification.
 * @param {string} message The message to display.
 * @param {'success'|'error'} type The type of notification.
 */
function showNotification(message, type = 'success') {
    if (!notificationsContainer) return;
    const toastId = 'toast-' + Date.now();
    const toastHTML = `
        <div id="${toastId}" class="toast align-items-center text-white ${type === 'error' ? 'bg-danger' : 'bg-success'} border-0" role="alert" aria-live="assertive" aria-atomic="true">
            <div class="d-flex"><div class="toast-body">${message}</div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button></div>
        </div>`;
    notificationsContainer.insertAdjacentHTML('beforeend', toastHTML);
    const toastElement = document.getElementById(toastId);
    const toast = new bootstrap.Toast(toastElement, { delay: 5000 });
    toast.show();
    toastElement.addEventListener('hidden.bs.toast', () => toastElement.remove());
}

/**
 * Handles user logout.
 */
async function handleLogout() {
    try {
        await fetch('/api/logout', { method: 'POST' });
        showNotification('Logged out successfully.', 'success');
        setTimeout(() => window.location.href = "/", 500);
    } catch (error) {
        showNotification('Logout failed.', 'error');
        setTimeout(() => window.location.href = "/", 500);
    }
}

/**
 * Populates the sidebar with navigation links.
 * @param {object} user The current user object ({ username, role }).
 */
async function populateSidebar(user) {
    if (!sidebarNav) return;

    const workplacesResponse = await fetch('/api/workplaces');
    const workplaces = await workplacesResponse.json();

    const currentPath = window.location.pathname;

    let adminLinks = '';
    if (user.role === 'administrator') {
        const isAdminPage = ['/admin.html', '/columns.html', '/queries.html', '/manage-workplaces.html', '/users.html', '/history.html'].includes(currentPath);
        adminLinks = `<a href="/admin.html" class="nav-link ${isAdminPage ? 'active' : ''}">Administration</a>`;
    }

    const workplaceLinks = workplaces.map(w => {
        // Treat "My Dashboard" like any other workplace, but ensure its link points to the root.
        const isMyDashboard = w.name === "My Dashboard";
        const link = isMyDashboard ? '/' : `/workplaces/${w.id}`;
        const isActive = isMyDashboard ? (currentPath === '/') : (currentPath === `/workplaces/${w.id}`);
        return `<a href="${link}" class="nav-link ${isActive ? 'active' : ''}">${w.name}</a>`;
    }).join('');

    sidebarNav.innerHTML = `
        <div class="sidebar-header">
            <h2 class="mb-1">Bugzilla Tracker</h2>
            <div id="scheduler-status-container">
                <!-- Status will be loaded here -->
            </div>
            <a href="/execution.html" class="nav-link mt-2 ${currentPath === '/execution.html' ? 'active' : ''}">Update Service Execution</a>
        </div>

        <div class="nav-container d-flex flex-column">
            <div>
                <div class="nav-category">Workplaces</div>
                <div class="px-3 mb-2">
                    <input type="search" id="workplace-search" class="form-control form-control-sm" placeholder="Filter workplaces...">
                </div>
                <div id="workplace-links-container">${workplaceLinks}</div>
            </div>
            <div class="mt-auto">
                ${adminLinks}
            </div>
        </div>
    `;

    // Add search functionality to the sidebar
    const searchInput = document.getElementById('workplace-search');
    const linksContainer = document.getElementById('workplace-links-container');
    const allLinks = Array.from(linksContainer.getElementsByTagName('a'));
    searchInput.addEventListener('input', (e) => {
        const searchTerm = e.target.value.toLowerCase();
        allLinks.forEach(link => {
            link.style.display = link.textContent.toLowerCase().includes(searchTerm) ? '' : 'none';
        });
    });
}

/**
 * Checks login status and initializes the page.
 * If not logged in, redirects to the login page.
 * @param {Function} onLoggedIn - A callback function to run after successful login verification.
 * @param {string} [pageTitle=document.title] - An optional title for the page header.
 */
async function initializePage(onLoggedIn, pageTitle) {
    try {
        const response = await fetch('/api/users/me');
        if (!response.ok) {
            // If not on the login page, redirect there.
            if (window.location.pathname !== '/') window.location.href = '/';
            return; // Stop initialization
        }
        const user = await response.json();
        await populateSidebar(user);

        // Reveal the main content area now that everything is ready.
        const mainContent = document.getElementById('main-page-content');
        if (mainContent) mainContent.style.visibility = 'visible';

        // --- User Info Dropdown ---
        const userRole = user.role.charAt(0).toUpperCase() + user.role.slice(1);
        const userDropdownHtml = `
            <div class="user-info-dropdown dropdown">
                <button class="btn dropdown-toggle" type="button" id="userDropdown" data-bs-toggle="dropdown" aria-expanded="false">
                    <strong>${user.username}</strong> (${userRole})
                </button>
                <ul class="dropdown-menu dropdown-menu-end" aria-labelledby="userDropdown">
                    <li><button class="dropdown-item logout" id="logout-btn">Logout</button></li>
                </ul>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', userDropdownHtml);
        document.getElementById('logout-btn').addEventListener('click', handleLogout);

        // --- Dynamic Header Creation ---
        const finalTitle = pageTitle || document.title;
        const headerHtml = `
            <div class="d-flex align-items-center mb-4">
                <h1 class="h2 mb-0" id="main-page-title">${finalTitle}</h1>
            </div>
        `;
        mainContent.insertAdjacentHTML('afterbegin', headerHtml);

        // --- Sidebar Toggle Logic ---
        // Create the toggle button and its container and add it directly to the body.
        const toggleContainer = document.createElement('div');
        toggleContainer.className = 'sidebar-toggle-container';
        toggleContainer.innerHTML = `<button id="sidebar-toggle" title="Toggle Sidebar">&laquo;</button>`;
        document.body.appendChild(toggleContainer);

        document.getElementById('sidebar-toggle').addEventListener('click', () => {
            document.body.classList.toggle('sidebar-collapsed');
            localStorage.setItem('sidebarCollapsed', document.body.classList.contains('sidebar-collapsed'));
        });

        // Initial check and periodic update for scheduler status
        checkSchedulerStatus();
        setInterval(checkSchedulerStatus, 60000); // Check every 60 seconds

        if (onLoggedIn) onLoggedIn(user);
    } catch (error) {
        console.error("Initialization failed:", error);
        if (window.location.pathname !== '/') window.location.href = '/';
    }
}

async function checkSchedulerStatus() {
    const statusContainer = document.getElementById('scheduler-status-container');
    if (!statusContainer) return;

    try {
        const response = await fetch('/api/service-status/scheduler');
        const data = await response.json();
        const statusClass = data.status === 'online' ? 'online' : 'offline';
        const statusText = data.status === 'online' ? 'Online' : 'Offline';
        statusContainer.innerHTML = `<span class="scheduler-status ${statusClass}">Update Service: ${statusText}</span>`;
    } catch (error) {
        statusContainer.innerHTML = `<span class="scheduler-status offline">Update Service: Offline</span>`;
        console.error("Could not fetch scheduler status:", error);
    }
}

// Check for saved sidebar state on every page load
if (localStorage.getItem('sidebarCollapsed') === 'true') {
    document.body.classList.add('sidebar-collapsed');
}