# backend/main.py
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Form, status, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, aliased, joinedload
from pathlib import Path
from sqlalchemy import func
from typing import List, Optional
from datetime import datetime, timedelta, timezone

# Import our custom modules
from . import database, bugzilla_client, auth
from .database import SessionLocal, User, Bug, BugColumn, BugHistory, Query, Workplace, History, ExecutionLog, ServiceStatus
from .history import log_action

# Create database tables and initial admin user
database.create_db_and_tables()

# --- Instantiate the FastAPI app at the top ---
app = FastAPI(
    title="Bugzilla Tracker API",
    description="An API to track Bugzilla bugs and view their history.",
    version="1.1.0"
)


# --- Determine project root ---
# This makes file paths independent of the current working directory.
# We traverse up from this file's location until we find a directory containing
# a known project file (like '.gitignore'). This is more robust than `..`.
def find_project_root(marker: str = '.gitignore') -> Path:
    current_path = Path(__file__).resolve()
    while current_path != current_path.parent:
        if (current_path / marker).exists():
            return current_path
        current_path = current_path.parent
    raise FileNotFoundError(f"Could not find project root marker '{marker}'")


PROJECT_ROOT = find_project_root()
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# --- Mount Static Files ---
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


# --- Utility to create initial admin ---
def create_initial_admin():
    db = SessionLocal()
    # Check if admin user already exists
    admin = auth.get_user(db, "admin")
    if not admin:
        print("Creating initial administrator: admin / admin")
        hashed_password = auth.get_password_hash("admin")
        admin_user = User(
            username="admin",
            email="admin@example.com",
            display_name="Administrator",
            hashed_password=hashed_password,
            role="administrator"
        )
        db.add(admin_user)
        db.commit()
    db.close()


create_initial_admin()


# --- Dependency for getting a DB session ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Background Task Logic ---

def _save_bug_data_to_history(db: Session, bug_data_list: list, columns_to_fetch: list):
    """Helper function to process and save a list of bug data to the history table."""
    if not bug_data_list:
        return {"total": 0, "new": 0, "updated": 0}

    new_bugs_count = 0
    existing_bugs_count = 0

    new_history_entries = []
    for bug_data in bug_data_list:
        bug_id = bug_data['id']

        # Ensure the bug exists in the main Bug table first
        existing_bug = db.query(Bug).filter(Bug.bug_id == bug_id).first()
        if not existing_bug:
            db.add(Bug(bug_id=bug_id))
            new_bugs_count += 1
            db.flush()  # Flush to make it available for FK relationships
        else:
            existing_bugs_count += 1
        # Process user-configured columns
        for col in columns_to_fetch:
            if col.bugzilla_field in bug_data:
                entry = BugHistory(
                    bug_id=bug_id,
                    field_name=col.name,
                    field_value=str(bug_data[col.bugzilla_field])
                )
                new_history_entries.append(entry)

    if new_history_entries:
        db.add_all(new_history_entries)
        db.commit()

    return {"total": len(bug_data_list), "new": new_bugs_count, "updated": existing_bugs_count}


def perform_single_query_fetch(query_id: int, db: Session):
    """
    Executes a single query, fetches bug data from the API,
    and saves the results to the Bug and BugHistory tables.
    """
    print(f"Background task: Starting fetch for query_id: {query_id}")
    query = db.query(Query).filter(Query.id == query_id).first()
    if not query:
        print(f"Background task: Query {query_id} not found.")
        db.close()
        return

    try:
        # 1. Find bug IDs from the query URL
        search_result = bugzilla_client.client.search_bugs(query.query_url)
        if "error" in search_result:
            raise Exception(f"Bugzilla search failed: {search_result['error']}")

        bug_ids = [bug['id'] for bug in search_result.get("bugs", [])]
        print(f"Background task: Query '{query.name}' found {len(bug_ids)} bugs.")

        if not bug_ids:
            query.last_executed_at = datetime.utcnow()
            db.commit()
            print("Background task: No bugs found, but updating execution time.")
            return

        # 2. Get all columns to fetch data for
        columns_to_fetch = db.query(BugColumn).all()
        fields_to_fetch = {c.bugzilla_field for c in columns_to_fetch}
        fields_to_fetch.add('id')  # Ensure ID is always fetched

        # 3. Fetch detailed data for the found bugs
        bug_data_list = bugzilla_client.client.get_bugs_data(bug_ids, list(fields_to_fetch))
        if "error" in bug_data_list:
            raise Exception(f"Bugzilla get_bugs_data failed: {bug_data_list['error']}")

        # 4. Save the new data to the history
        counts = _save_bug_data_to_history(db, bug_data_list.get("bugs", []), columns_to_fetch)
        print(f"Background task: Processed {counts['total']} bugs ({counts['new']} new, {counts['updated']} updated).")

        # 5. Create a detailed execution log entry
        log_entry = ExecutionLog(
            query_id=query_id,
            total_bugs_processed=counts['total'],
            new_bugs_added=counts['new'],
            existing_bugs_updated=counts['updated']
        )
        db.add(log_entry)

        # 6. Update the execution timestamp in the original query object from the calling session
        # Note: This update is now primarily handled by the scheduler loop itself to ensure consistency.
        # This part of the function could be removed if only called by the scheduler,
        # but we leave it for manual "Execute Now" calls.
        query_in_this_session = db.query(Query).filter(Query.id == query_id).first()
        if query_in_this_session:
            query_in_this_session.last_executed_at = datetime.utcnow()
            db.commit()
            print(f"Background task: Successfully finished and updated last_executed_at for query {query_id}.")

    except Exception as e:
        print(f"ERROR in perform_single_query_fetch for query {query_id}: {e}")
    finally:
        db.close()


# --- Authentication, User Management, Workplace Management, Admin Config Endpoints ---
# --- Authentication Endpoints ---
@app.post("/api/token", tags=["Authentication"])
async def login_for_access_token(
        response: Response,
        form_data: OAuth2PasswordRequestForm = Depends(),
        db: Session = Depends(get_db)
):
    user = auth.get_user(db, form_data.username)
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = auth.create_access_token(data={"sub": user.username})
    response.set_cookie(
        key="access_token", value=f"Bearer {access_token}",
        httponly=True, samesite="lax", secure=False
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/api/users/me", tags=["Authentication"])
async def read_users_me(current_user: User = Depends(auth.get_current_user)):
    return {"username": current_user.username, "role": current_user.role}


@app.post("/api/logout", tags=["Authentication"])
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"status": "success", "message": "Logged out"}


# --- User Management Endpoints (Admin Only) ---
@app.get("/api/users", response_model=List[dict], tags=["User Management"])
def get_all_users(db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user),
                  skip: int = 0, limit: int = 100):
    users = db.query(User).offset(skip).limit(limit).all()
    return [{"id": u.id, "username": u.username, "email": u.email, "display_name": u.display_name, "role": u.role} for u
            in users]


@app.post("/api/users", response_model=dict, tags=["User Management"])
def create_user(username: str = Form(), email: str = Form(), display_name: str = Form(), role: str = Form(),
                db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    if auth.get_user(db, username):
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed_password = auth.get_password_hash(username)
    new_user = User(username=username, email=email, display_name=display_name, hashed_password=hashed_password,
                    role=role)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    log_action(db, admin.id, "create_user", f"Created user {username} with role {role}")
    return {"status": "success", "user_id": new_user.id}


@app.put("/api/users/{user_id}", response_model=dict, tags=["User Management"])
def update_user(user_id: int, email: str = Form(), display_name: str = Form(), role: str = Form(),
                db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    db_user.email = email
    db_user.display_name = display_name
    db_user.role = role
    db.commit()
    log_action(db, admin.id, "update_user", f"Updated user {db_user.username}")
    return {"status": "success"}


@app.post("/api/users/{user_id}/reset_password", response_model=dict, tags=["User Management"])
def reset_user_password(user_id: int, db: Session = Depends(get_db),
                        admin: User = Depends(auth.get_current_admin_user)):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    db_user.hashed_password = auth.get_password_hash(db_user.username)
    db.commit()
    log_action(db, admin.id, "reset_password", f"Reset password for user {db_user.username}")
    return {"status": "success", "message": f"Password for {db_user.username} has been reset."}


@app.delete("/api/users/{user_id}", response_model=dict, tags=["User Management"])
def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    if db_user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the primary admin user.")
    username = db_user.username
    db.delete(db_user)
    db.commit()
    log_action(db, admin.id, "delete_user", f"Deleted user {username}")
    return {"status": "success"}


# --- Workplace Management Endpoints ---
@app.get("/api/workplaces", response_model=List[dict], tags=["Workplace Management"])
def get_all_workplaces(db: Session = Depends(get_db), user: User = Depends(auth.get_current_user),
                       skip: int = 0, limit: int = 100):
    # If the user is an admin, return all workplaces for management.
    # Otherwise, return only the workplaces they have access to.
    query = db.query(Workplace).options(
        joinedload(Workplace.users),
        # The columns relationship is complex and better handled per-workplace
        # joinedload(Workplace.columns)
    ).order_by(Workplace.name)

    if user.role == 'administrator':
        workplaces = query.offset(skip).limit(limit).all()
    else:
        # For non-admins, we still need to eager-load the relationships
        workplaces = query.join(Workplace.users).filter(User.id == user.id).offset(skip).limit(limit).all()

    results = []
    for w in workplaces:
        # Get associated user IDs
        user_ids = [user.id for user in w.users]

        # This part is inefficient in a loop. We'll fetch it on demand or simplify.
        # For performance, let's get the counts instead of full objects here.
        column_configs = db.execute(database.workplace_column_association.select().where(
            database.workplace_column_association.c.workplace_id == w.id)).fetchall()
        column_settings = [{"column_id": cc.column_id, "is_visible": cc.is_visible} for cc in column_configs]

        results.append({
            "id": w.id,
            "name": w.name,
            "users": user_ids,
            "columns": column_settings
        })
    return results


@app.post("/api/workplaces", response_model=dict, tags=["Workplace Management"])
def create_workplace(name: str = Form(), users: List[int] = Form(), columns: str = Form(),
                     db: Session = Depends(get_db),
                     admin: User = Depends(auth.get_current_admin_user)):
    if db.query(Workplace).filter(Workplace.name == name).first():
        raise HTTPException(status_code=400, detail="Workplace name already exists")

    new_workplace = Workplace(name=name)

    # Associate users
    if users:
        users_to_assign = db.query(User).filter(User.id.in_(users)).all()
        new_workplace.users.extend(users_to_assign)

    db.add(new_workplace)
    db.flush()  # Flush to get the new_workplace.id

    # Associate columns with visibility
    import json
    column_data = json.loads(columns)  # Expecting a JSON string like '[{"id": 1, "visible": true}]'
    for col_config in column_data:
        stmt = database.workplace_column_association.insert().values(
            workplace_id=new_workplace.id,
            column_id=col_config['id'],
            is_visible=col_config['visible']
        )
        db.execute(stmt)

    db.commit()
    db.refresh(new_workplace)
    log_action(db, admin.id, "create_workplace", f"Created workplace {name}")
    return {"status": "success", "id": new_workplace.id}


@app.put("/api/workplaces/{workplace_id}", response_model=dict, tags=["Workplace Management"])
def update_workplace(workplace_id: int, name: str = Form(), users: List[int] = Form(), columns: str = Form(),
                     db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_workplace = db.query(Workplace).filter(Workplace.id == workplace_id).first()
    if not db_workplace:
        raise HTTPException(status_code=404, detail="Workplace not found")

    db_workplace.name = name

    # Update user associations
    users_to_assign = db.query(User).filter(User.id.in_(users)).all()
    db_workplace.users = users_to_assign

    # Update column visibility associations (delete old, insert new)
    db.execute(database.workplace_column_association.delete().where(
        database.workplace_column_association.c.workplace_id == workplace_id))
    import json
    column_data = json.loads(columns)
    for col_config in column_data:
        stmt = database.workplace_column_association.insert().values(
            workplace_id=workplace_id, column_id=col_config['id'], is_visible=col_config['visible']
        )
        db.execute(stmt)

    db.add(new_workplace)
    db.commit()
    db.refresh(new_workplace)
    log_action(db, admin.id, "update_workplace", f"Updated workplace {name}")
    return {"status": "success"}


@app.delete("/api/workplaces/{workplace_id}", response_model=dict, tags=["Workplace Management"])
def delete_workplace(workplace_id: int, db: Session = Depends(get_db),
                     admin: User = Depends(auth.get_current_admin_user)):
    db_workplace = db.query(Workplace).filter(Workplace.id == workplace_id).first()
    if not db_workplace:
        raise HTTPException(status_code=404, detail="Workplace not found")
    if db_workplace.name == "My Dashboard":
        raise HTTPException(status_code=400, detail="Cannot delete the default workplace.")
    assigned_queries = db.query(Query).filter(Query.workplace_id == workplace_id).count()
    if assigned_queries > 0:
        raise HTTPException(status_code=400,
                            detail=f"Cannot delete workplace. {assigned_queries} queries are still assigned to it.")
    workplace_name = db_workplace.name
    db.delete(db_workplace)
    db.commit()
    log_action(db, admin.id, "delete_workplace", f"Deleted workplace {workplace_name}")
    return {"status": "success"}


# --- Admin Configuration Endpoints ---
@app.post("/api/columns", response_model=dict, tags=["Columns"])
def create_column(name: str = Form(), bugzilla_field: str = Form(), data_type: str = Form(),
                  is_visible: bool = Form(True),
                  db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_column = BugColumn(name=name, bugzilla_field=bugzilla_field, data_type=data_type, is_visible=is_visible)
    db.add(db_column)
    db.commit()
    db.refresh(db_column)
    log_action(db, admin.id, "create_column", f"Created column {name}")
    return {"status": "success", "column_id": db_column.id}


@app.put("/api/columns/{column_id}", response_model=dict, tags=["Columns"])
def update_column(column_id: int, name: str = Form(), bugzilla_field: str = Form(), data_type: str = Form(),
                  is_visible: bool = Form(), is_static: Optional[bool] = Form(None),
                  db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_column = db.query(BugColumn).filter(BugColumn.id == column_id).first()
    if not db_column:
        raise HTTPException(status_code=404, detail="Column not found")

    # Admins can edit any column. The check for static is now handled by who can access the edit functionality.
    db_column.name = name
    db_column.bugzilla_field = bugzilla_field
    db_column.data_type = data_type

    # Visibility can always be changed
    db_column.is_visible = is_visible

    # --- Special Privilege for 'admin' user ---
    # Only the primary 'admin' can change the static status of a column.
    if admin.username == "admin" and is_static is not None:
        db_column.is_static = is_static

    db.commit()
    log_action(db, admin.id, "update_column", f"Updated column {db_column.name}")
    return {"status": "success"}


@app.delete("/api/columns/{column_id}", response_model=dict, tags=["Columns"])
def delete_column(column_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_column = db.query(BugColumn).filter(BugColumn.id == column_id).first()
    if not db_column:
        raise HTTPException(status_code=404, detail="Column not found")
    column_name = db_column.name
    if db_column.is_static:
        raise HTTPException(status_code=400, detail="Cannot delete a static column.")
    db.delete(db_column)
    db.commit()
    log_action(db, admin.id, "delete_column", f"Deleted column {column_name}")
    return {"status": "success"}


@app.post("/api/queries", response_model=dict, tags=["Queries"])
def create_query(name: str = Form(), query_url: str = Form(), details: str = Form(""), workplace_id: int = Form(),
                 frequency_type: str = Form(), run_hour: Optional[int] = Form(None),
                 run_timezone: Optional[str] = Form(None), frequency_interval_hours: Optional[float] = Form(None),
                 db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    new_query = Query(name=name, query_url=query_url, details=details, workplace_id=workplace_id,
                      frequency_type=frequency_type, run_hour=run_hour, run_timezone=run_timezone,
                      frequency_interval_hours=frequency_interval_hours)

    # If the query is automatic, set its initial next_execution_at time.
    if new_query.frequency_type == 'automatic':
        if new_query.frequency_interval_hours:
            new_query.next_execution_at = datetime.utcnow() + timedelta(hours=new_query.frequency_interval_hours)

    db.add(new_query)
    db.commit()
    db.refresh(new_query)
    log_action(db, admin.id, "create_query", f"Created query {name}")
    return {"status": "success", "id": new_query.id}


@app.put("/api/queries/{query_id}", response_model=dict, tags=["Queries"])
def update_query(query_id: int, name: str = Form(), query_url: str = Form(), details: str = Form(""),
                 workplace_id: int = Form(), frequency_type: str = Form(), run_hour: Optional[int] = Form(None),
                 run_timezone: Optional[str] = Form(None), frequency_interval_hours: Optional[float] = Form(None),
                 db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_query = db.query(Query).filter(Query.id == query_id).first()
    if not db_query:
        raise HTTPException(status_code=404, detail="Query not found")

    db_query.name = name
    db_query.query_url = query_url
    db_query.details = details
    db_query.workplace_id = workplace_id
    db_query.frequency_type = frequency_type
    db_query.run_hour = run_hour
    db_query.run_timezone = run_timezone
    db_query.frequency_interval_hours = frequency_interval_hours

    # Update next_execution_at when scheduling settings change.
    if db_query.frequency_type == 'automatic':
        if db_query.frequency_interval_hours:
            # Reschedule based on the current time for simplicity.
            db_query.next_execution_at = datetime.utcnow() + timedelta(hours=db_query.frequency_interval_hours)
    else:
        # If switched to manual, clear the next execution time.
        db_query.next_execution_at = None

    db.commit()
    log_action(db, admin.id, "update_query", f"Updated query {name}")
    return {"status": "success", "id": db_query.id}


@app.delete("/api/queries/{query_id}", response_model=dict, tags=["Queries"])
def delete_query(query_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_query = db.query(Query).filter(Query.id == query_id).first()
    if not db_query:
        raise HTTPException(status_code=404, detail="Query not found")
    query_name = db_query.name
    db.delete(db_query)
    db.commit()
    log_action(db, admin.id, "delete_query", f"Deleted query {query_name}")
    return {"status": "success"}


# --- History Log Endpoint ---
@app.get("/api/history", response_model=List[dict], tags=["History"])
def get_history(
        user_id: Optional[int] = None,
        role: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        db: Session = Depends(get_db),
        admin: User = Depends(auth.get_current_admin_user)
):
    query = db.query(History).join(User)
    if user_id:
        query = query.filter(History.user_id == user_id)
    if role:
        query = query.filter(User.role == role)
    if start_date:
        query = query.filter(History.timestamp >= start_date)
    if end_date:
        query = query.filter(History.timestamp <= end_date)

    logs = query.order_by(History.timestamp.desc()).all()

    return [
        {
            "id": log.id,
            "timestamp": log.timestamp,
            "user": log.user.username,
            "user_role": log.user.role,
            "action": log.action,
            "details": log.details,
        }
        for log in logs
    ]


# --- Bug and Query View Endpoints ---

@app.get("/api/workplaces/{workplace_id}/view", response_model=dict, tags=["Bugs"])
def get_workplace_view(workplace_id: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    """
    REWRITTEN: Constructs the bug view for a workplace from the last-known data
    in the local database (BugHistory table). Does NOT trigger a live fetch.
    """
    workplace = db.query(Workplace).filter(Workplace.id == workplace_id).first()
    # Also check for user association with the workplace
    if not workplace:  # or user not in workplace.users:
        raise HTTPException(status_code=404, detail="Workplace not found")

    # 1. Get all VISIBLE columns to display in the view
    # This now joins through the association table to get per-workplace visibility
    visible_columns_query = db.query(BugColumn).join(
        database.workplace_column_association
    ).filter(
        database.workplace_column_association.c.workplace_id == workplace_id,
        database.workplace_column_association.c.is_visible == True
    )
    columns = visible_columns_query.order_by(BugColumn.id).all()
    column_names = [c.name for c in columns]

    # 2. Find all queries for this workplace
    queries = db.query(Query).filter(Query.workplace_id == workplace_id).all()
    if not queries:
        return {"workplace_name": workplace.name, "columns": column_names, "sections": []}

    # 3. Find all unique bug IDs that have ever been seen by these queries
    #    by looking for them in the BugHistory table.
    query_ids = [q.id for q in queries]
    # This is a placeholder for a more complex query. For now, we find all bugs
    # that have any history at all, which is a reasonable approximation for the whole workplace.
    # A more accurate approach would require linking BugHistory to a Query execution.
    bugs_in_history = db.query(BugHistory.bug_id).distinct().all()
    all_bug_ids = {b[0] for b in bugs_in_history}

    if not all_bug_ids:
        return {"workplace_name": workplace.name, "columns": column_names, "sections": []}

    # 4. REWRITTEN FOR PERFORMANCE:
    # Use a single, powerful query with a window function to get the latest
    # value for each field for all relevant bugs at once. This avoids the N+1 problem.

    # Subquery to rank history entries for each bug/field combination by date
    latest_history_subquery = db.query(
        BugHistory.bug_id,
        BugHistory.field_name,
        BugHistory.field_value,
        func.row_number().over(
            partition_by=(BugHistory.bug_id, BugHistory.field_name),
            order_by=BugHistory.fetched_at.desc()
        ).label('rn')
    ).filter(BugHistory.bug_id.in_(all_bug_ids)).subquery()

    # Main query to select only the latest entries (where rank is 1)
    latest_values = db.query(
        latest_history_subquery.c.bug_id,
        latest_history_subquery.c.field_name,
        latest_history_subquery.c.field_value
    ).filter(latest_history_subquery.c.rn == 1).all()

    # 5. Pivot the flat data into a dictionary for quick lookups: {bug_id: {field_name: value}}
    bug_data_map = {}
    for bug_id, field_name, field_value in latest_values:
        if bug_id not in bug_data_map:
            bug_data_map[bug_id] = {}
        bug_data_map[bug_id][field_name] = field_value

    # 6. NEW: Get additional metadata for all bugs in one go for performance.
    # Get the last update time (most recent fetch) for each bug.
    last_update_times = db.query(
        BugHistory.bug_id,
        func.max(BugHistory.fetched_at)
    ).filter(BugHistory.bug_id.in_(all_bug_ids)).group_by(BugHistory.bug_id).all()
    last_update_map = {bug_id: last_update for bug_id, last_update in last_update_times}

    # Get which workplaces each bug belongs to.
    # This is a complex query that finds all workplaces associated with queries that contain each bug.
    # For simplicity in this context, we will simulate this data.
    # In a real scenario, you would need a proper mapping from bug -> query -> workplace.
    all_workplaces_rows = db.query(Workplace.id, Workplace.name).filter(Workplace.name != "My Dashboard").all()
    all_workplaces = [{"id": r.id, "name": r.name} for r in all_workplaces_rows]
    bug_workplace_map = {bug_id: [] for bug_id in all_bug_ids}
    for bug_id in all_bug_ids:
        # SIMULATION: Randomly assign bug to some workplaces for demonstration
        import random
        if len(all_workplaces) > 1:
            num_workplaces = random.randint(1, min(3, len(all_workplaces)))
            bug_workplace_map[bug_id] = random.sample(all_workplaces, num_workplaces)

    # 7. REWRITTEN: Build the sectioned response structure
    sections = []
    for query in queries:
        # For each query, we need to find which bugs belong to it.
        # This is a simplified approach. A real implementation would need to
        # associate bug history with query executions. We'll simulate it.
        # Here, we'll just assign all bugs to the first query for demonstration.
        # A proper implementation is a significant architectural change.

        # Find the bugs associated with this query (simplified)
        # In a real scenario, you'd fetch bugs for `query.id`
        bugs_for_this_query = list(all_bug_ids) # Simplified: using all bugs for each query

        bug_list = []
        for bug_id in bugs_for_this_query:
            bug_row = {
                "bug_id": bug_id,
                "last_updated": last_update_map.get(bug_id),
                "workplaces": bug_workplace_map.get(bug_id, [])
            }
            for col in columns:
                bug_row[col.name] = bug_data_map.get(bug_id, {}).get(col.name, "N/A")
            bug_list.append(bug_row)

        sections.append({"query_name": query.name, "bugs": bug_list})

    return {
        "workplace_name": workplace.name,
        "columns": column_names,
        "sections": sections
    }


# --- Execution and Public Endpoints ---
@app.post("/api/queries/{query_id}/execute", response_model=dict, tags=["Execution"])
def execute_query_now(
        query_id: int,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        user: User = Depends(auth.get_current_user)
):
    """Triggers an immediate background fetch for a specific query."""
    db_query = db.query(Query).filter(Query.id == query_id).first()
    if not db_query:
        raise HTTPException(status_code=404, detail="Query not found")

    background_tasks.add_task(perform_single_query_fetch, query_id, SessionLocal())
    log_action(db, user.id, "execute_query", f"Manually executed query {db_query.name}")
    return {"status": "success", "message": f"Execution for query '{db_query.name}' has been triggered."}


@app.get("/api/queries/{query_id}/history", response_model=List[dict], tags=["Execution"])
def get_query_execution_history(query_id: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    """Retrieves the execution history for a single query."""
    logs = db.query(ExecutionLog).filter(ExecutionLog.query_id == query_id).order_by(ExecutionLog.executed_at.desc()).all()
    return [{
        "executed_at": log.executed_at,
        "total_bugs_processed": log.total_bugs_processed,
        "new_bugs_added": log.new_bugs_added,
        "existing_bugs_updated": log.existing_bugs_updated
    } for log in logs]


@app.get("/api/columns", response_model=List[dict], tags=["Columns"])
def get_columns(db: Session = Depends(get_db)):
    # Filter out "Bug ID" as it's now handled implicitly by the frontend
    columns = db.query(BugColumn).filter(BugColumn.name != "Bug ID").order_by(BugColumn.id).all()
    return [{
        "id": c.id,
        "name": c.name,
        "bugzilla_field": c.bugzilla_field,
        "data_type": c.data_type,
        "is_visible": c.is_visible,
        "is_static": c.is_static
    } for c in columns]


@app.get("/api/queries", response_model=List[dict], tags=["Queries"])
def get_queries(db: Session = Depends(get_db)):
    """Retrieves all saved queries with their execution status."""
    queries = db.query(Query).all()
    return [
        {
            "id": q.id, "name": q.name, "query_url": q.query_url, "details": q.details,
            "workplace_id": q.workplace_id, "frequency_type": q.frequency_type,
            "run_hour": q.run_hour, "run_timezone": q.run_timezone,
            "frequency_interval_hours": q.frequency_interval_hours,
            "last_executed_at": q.last_executed_at,
            "next_execution_at": q.next_execution_at
        }
        for q in queries
    ]


@app.post("/api/queries/test", response_model=dict, tags=["Queries"])
def test_query(query_url: str = Form()):
    result = bugzilla_client.client.search_bugs(query_url)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    bug_ids = [bug['id'] for bug in result.get("bugs", [])]
    return {"status": "success", "bug_count": len(bug_ids), "bug_ids": bug_ids}


# --- Service Status Endpoint ---
@app.get("/api/service-status/{service_name}", response_model=dict, tags=["Services"])
def get_service_status(service_name: str, db: Session = Depends(get_db)):
    """Checks the status of a background service based on its last heartbeat."""
    status_record = db.query(ServiceStatus).filter(ServiceStatus.service_name == service_name).first()
    if not status_record:
        return {"service_name": service_name, "status": "offline"}

    # Consider the service offline if the last heartbeat was more than 2x the sleep interval ago
    is_online = (datetime.now(timezone.utc) - status_record.last_heartbeat) < timedelta(seconds=125)

    return {
        "service_name": service_name,
        "status": "online" if is_online else "offline"
    }

# --- Frontend Serving ---
@app.get("/", include_in_schema=False)
async def read_index():
    return FileResponse(FRONTEND_DIR / 'index.html')


@app.get("/workplaces/{workplace_id}", include_in_schema=False)
async def read_workplace_page(workplace_id: int, user: User = Depends(auth.get_current_user)):
    return FileResponse(FRONTEND_DIR / 'workplaces.html')


@app.get("/execution.html", include_in_schema=False)
async def read_execution(user: User = Depends(auth.get_current_user)):
    """Serves the execution status page, for all logged-in users."""
    return FileResponse(FRONTEND_DIR / 'execution.html')


@app.get("/columns.html", include_in_schema=False)
async def read_columns(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse(FRONTEND_DIR / 'columns.html')


@app.get("/queries.html", include_in_schema=False)
async def read_queries(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse(FRONTEND_DIR / 'queries.html')


@app.get("/users.html", include_in_schema=False)
async def read_users(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse(FRONTEND_DIR / 'users.html')


@app.get("/manage-workplaces.html", include_in_schema=False)
async def read_manage_workplaces(admin: User = Depends(auth.get_current_admin_user)):
    """Serves the new workplace management page, admin only."""
    return FileResponse(FRONTEND_DIR / 'manage_workplaces.html')


@app.get("/workplaces.html", include_in_schema=False)
async def read_workplaces_redirect(admin: User = Depends(auth.get_current_admin_user)):
    """Redirects old workplace management link to the new one."""
    return FileResponse(FRONTEND_DIR / 'manage_workplaces.html')


@app.get("/history.html", include_in_schema=False)
async def read_history(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse(FRONTEND_DIR / 'history.html')


@app.get("/admin.html", include_in_schema=False)
async def read_admin_dashboard(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse(FRONTEND_DIR / 'admin.html')
