# Bugzilla Triage Tracker

A self-hosted web application designed to track and monitor the status of Bugzilla bugs over time. It allows teams to create custom dashboards ("Workplaces") by grouping specific Bugzilla queries, and it periodically fetches data to provide a historical view of bug changes.

This tool is ideal for QA teams, engineering managers, and anyone who needs to monitor the state of multiple bugs without constantly re-running queries in Bugzilla's native UI.

## Key Features

*   **Custom Dashboards (Workplaces):** Group related Bugzilla queries into logical workplaces for different teams or projects.
*   **Automated & Manual Fetching:** Configure queries to run automatically on a schedule (e.g., every 24 hours) or trigger them manually for up-to-the-minute data.
*   **Historical Data Tracking:** The application saves a snapshot of bug data each time a query is executed, allowing you to see how bug fields have changed over time.
*   **Dynamic Column Configuration:** Administrators can define which Bugzilla fields (including custom fields) to fetch and display in the bug views.
*   **Role-Based Access Control:**
    *   **Administrator:** Full control over user management, query configuration, column definitions, and workplaces.
    *   **User:** Can view bug data within workplaces and trigger manual query executions.
*   **Modern Web Interface:** A clean, responsive UI built with Bootstrap for easy navigation and management.
*   **Secure Authentication:** Uses JWT-based authentication with `HttpOnly` cookies.

## Technology Stack

The project is built with a modern Python backend and a vanilla JavaScript frontend.

#### Backend
*   **Framework:** FastAPI
*   **Web Server:** Uvicorn
*   **Database ORM:** SQLAlchemy
*   **Database:** SQLite (by default, easily configurable)
*   **Bugzilla Integration:** python-bugzilla
*   **Authentication:** Passlib & python-jose for password hashing and JWTs.

#### Frontend
*   **Framework:** None (Vanilla JavaScript)
*   **Styling:** Bootstrap 5 (via CDN)
*   **Core Technologies:** HTML5, modern JavaScript (ES6+)

## Getting Started

Follow these steps to set up and run the project on your local machine.

### 1. Prerequisites

*   **Python 3.8+** installed on your system.
*   Access to a command-line terminal (Command Prompt, PowerShell, or Bash).

### 2. Installation

1.  **Clone the Repository (Optional)**
    If you have the project as a zip file, simply extract it. Otherwise, clone it using Git:
    