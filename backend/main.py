# backend/main.py
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Form, status, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func
from typing import List, Optional
from datetime import datetime, timedelta

# Import our custom modules
from . import database, bugzilla_client, auth
from .database import SessionLocal, User, Bug, BugColumn, BugHistory, Query, Workplace

# Create database tables and initial admin user
database.create_db_and_tables()

# --- Instantiate the FastAPI app at the top ---
app = FastAPI(
    title="Bugzilla Tracker API",
    description="An API to track Bugzilla bugs and view their history.",
    version="1.1.0"
)


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
        return 0

    new_history_entries = []
    for bug_data in bug_data_list:
        bug_id = bug_data['id']

        # Ensure the bug exists in the main Bug table first
        existing_bug = db.query(Bug).filter(Bug.bug_id == bug_id).first()
        if not existing_bug:
            db.add(Bug(bug_id=bug_id))
            db.flush() # Flush to make it available for FK relationships

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

    return len(bug_data_list)


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
        fields_to_fetch.add('id') # Ensure ID is always fetched

        # 3. Fetch detailed data for the found bugs
        bug_data_list = bugzilla_client.client.get_bugs_data(bug_ids, list(fields_to_fetch))
        if "error" in bug_data_list:
            raise Exception(f"Bugzilla get_bugs_data failed: {bug_data_list['error']}")

        # 4. Save the new data to the history
        updated_count = _save_bug_data_to_history(db, bug_data_list.get("bugs", []), columns_to_fetch)
        print(f"Background task: Saved/updated history for {updated_count} bugs.")

        # 5. Update the execution timestamp in the original query object from the calling session
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
def get_all_users(db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    users = db.query(User).all()
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
    return {"status": "success"}


@app.post("/api/users/{user_id}/reset_password", response_model=dict, tags=["User Management"])
def reset_user_password(user_id: int, db: Session = Depends(get_db),
                        admin: User = Depends(auth.get_current_admin_user)):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    db_user.hashed_password = auth.get_password_hash(db_user.username)
    db.commit()
    return {"status": "success", "message": f"Password for {db_user.username} has been reset."}


@app.delete("/api/users/{user_id}", response_model=dict, tags=["User Management"])
def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    if db_user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the primary admin user.")
    db.delete(db_user)
    db.commit()
    return {"status": "success"}


# --- Workplace Management Endpoints ---
@app.get("/api/workplaces", response_model=List[dict], tags=["Workplace Management"])
def get_all_workplaces(db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    workplaces = db.query(Workplace).order_by(Workplace.name).all()
    return [{"id": w.id, "name": w.name} for w in workplaces]


@app.post("/api/workplaces", response_model=dict, tags=["Workplace Management"])
def create_workplace(name: str = Form(), db: Session = Depends(get_db),
                     admin: User = Depends(auth.get_current_admin_user)):
    if db.query(Workplace).filter(Workplace.name == name).first():
        raise HTTPException(status_code=400, detail="Workplace name already exists")
    new_workplace = Workplace(name=name)
    db.add(new_workplace)
    db.commit()
    db.refresh(new_workplace)
    return {"status": "success", "id": new_workplace.id}


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
    db.delete(db_workplace)
    db.commit()
    return {"status": "success"}


# --- Admin Configuration Endpoints ---
@app.post("/api/columns", response_model=dict, tags=["Columns"])
def create_column(name: str = Form(), bugzilla_field: str = Form(), data_type: str = Form(),
                  db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_column = BugColumn(name=name, bugzilla_field=bugzilla_field, data_type=data_type)
    db.add(db_column)
    db.commit()
    db.refresh(db_column)
    return {"status": "success", "column_id": db_column.id}


@app.delete("/api/columns/{column_id}", response_model=dict, tags=["Columns"])
def delete_column(column_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_column = db.query(BugColumn).filter(BugColumn.id == column_id).first()
    if not db_column:
        raise HTTPException(status_code=404, detail="Column not found")
    db.delete(db_column)
    db.commit()
    return {"status": "success"}


@app.post("/api/queries", response_model=dict, tags=["Queries"])
def create_query(name: str = Form(), query_url: str = Form(), details: str = Form(""), workplace_id: int = Form(),
                 frequency_type: str = Form(), run_hour: Optional[int] = Form(None),
                 run_timezone: Optional[str] = Form(None), frequency_interval_hours: Optional[int] = Form(None),
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
    return {"status": "success", "id": new_query.id}


@app.put("/api/queries/{query_id}", response_model=dict, tags=["Queries"])
def update_query(query_id: int, name: str = Form(), query_url: str = Form(), details: str = Form(""),
                 workplace_id: int = Form(), frequency_type: str = Form(), run_hour: Optional[int] = Form(None),
                 run_timezone: Optional[str] = Form(None), frequency_interval_hours: Optional[int] = Form(None),
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
    return {"status": "success", "id": db_query.id}


@app.delete("/api/queries/{query_id}", response_model=dict, tags=["Queries"])
def delete_query(query_id: int, db: Session = Depends(get_db), admin: User = Depends(auth.get_current_admin_user)):
    db_query = db.query(Query).filter(Query.id == query_id).first()
    if not db_query:
        raise HTTPException(status_code=404, detail="Query not found")
    db.delete(db_query)
    db.commit()
    return {"status": "success"}


# --- Bug and Query View Endpoints ---

@app.get("/api/workplaces/{workplace_id}/view", response_model=dict, tags=["Bugs"])
def get_workplace_view(workplace_id: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    """
    REWRITTEN: Constructs the bug view for a workplace from the last-known data
    in the local database (BugHistory table). Does NOT trigger a live fetch.
    """
    workplace = db.query(Workplace).filter(Workplace.id == workplace_id).first()
    if not workplace:
        raise HTTPException(status_code=404, detail="Workplace not found")

    # 1. Get all columns the user wants to see
    columns = db.query(BugColumn).order_by(BugColumn.id).all()
    column_names = [c.name for c in columns]

    # 2. Find all queries for this workplace
    queries = db.query(Query).filter(Query.workplace_id == workplace_id).all()
    if not queries:
        return {"workplace_name": workplace.name, "columns": column_names, "bugs": []}

    # 3. Find all unique bug IDs that have ever been seen by these queries
    #    by looking for them in the BugHistory table.
    query_ids = [q.id for q in queries]
    # This is a placeholder for a more complex query. For now, we find all bugs
    # that have any history at all, which is a reasonable approximation.
    # A more accurate approach would require linking BugHistory to a Query execution.
    bugs_in_history = db.query(BugHistory.bug_id).distinct().all()
    all_bug_ids = {b[0] for b in bugs_in_history}

    if not all_bug_ids:
        return {"workplace_name": workplace.name, "columns": column_names, "bugs": []}

    # 4. For each bug, get the latest value for each column from BugHistory
    results = []
    for bug_id in all_bug_ids:
        bug_row = {"bug_id": bug_id}
        for col in columns:
            latest_entry = db.query(BugHistory.field_value) \
                .filter(BugHistory.bug_id == bug_id, BugHistory.field_name == col.name) \
                .order_by(BugHistory.fetched_at.desc()) \
                .first()
            bug_row[col.name] = latest_entry[0] if latest_entry else "N/A"
        results.append(bug_row)

    return {
        "workplace_name": workplace.name,
        "columns": column_names,
        "bugs": results
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
    return {"status": "success", "message": f"Execution for query '{db_query.name}' has been triggered."}


@app.get("/api/columns", response_model=List[dict], tags=["Columns"])
def get_columns(db: Session = Depends(get_db)):
    columns = db.query(BugColumn).all()
    return [{"id": c.id, "name": c.name, "bugzilla_field": c.bugzilla_field, "data_type": c.data_type} for c in columns]


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


# --- Frontend Serving ---
@app.get("/", include_in_schema=False)
async def read_index():
    return FileResponse('frontend/index.html')


@app.get("/workplaces/{workplace_id}", include_in_schema=False)
async def read_workplace_page(workplace_id: int, user: User = Depends(auth.get_current_user)):
    return FileResponse('frontend/workplaces.html')


@app.get("/execution.html", include_in_schema=False)
async def read_execution(user: User = Depends(auth.get_current_user)):
    """Serves the execution status page, for all logged-in users."""
    return FileResponse('frontend/execution.html')


@app.get("/columns.html", include_in_schema=False)
async def read_columns(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse('frontend/columns.html')


@app.get("/queries.html", include_in_schema=False)
async def read_queries(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse('frontend/queries.html')


@app.get("/users.html", include_in_schema=False)
async def read_users(admin: User = Depends(auth.get_current_admin_user)):
    return FileResponse('frontend/users.html')


@app.get("/manage-workplaces.html", include_in_schema=False)
async def read_manage_workplaces(admin: User = Depends(auth.get_current_admin_user)):
    """Serves the new workplace management page, admin only."""
    return FileResponse('frontend/manage-workplaces.html')


@app.get("/workplaces.html", include_in_schema=False)
async def read_workplaces_redirect(admin: User = Depends(auth.get_current_admin_user)):
    """Redirects old workplace management link to the new one."""
    return FileResponse('frontend/manage-workplaces.html')