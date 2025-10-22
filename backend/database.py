# backend/database.py
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Float, ForeignKey, Table, Index
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func

# Import the centralized settings
from .config import settings

# --- Database Setup ---
# Use the database_url from the settings file
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Association Tables for Many-to-Many relationships ---

workplace_user_association = Table(
    'workplace_user_association', Base.metadata,
    Column('workplace_id', Integer, ForeignKey('workplaces.id'), primary_key=True),
    Column('user_id', Integer, ForeignKey('users.id'), primary_key=True)
)

workplace_column_association = Table(
    'workplace_column_association', Base.metadata,
    Column('workplace_id', Integer, ForeignKey('workplaces.id'), primary_key=True),
    Column('column_id', Integer, ForeignKey('bug_columns.id'), primary_key=True),
    Column('is_visible', Boolean, default=True)  # Per-workplace visibility
)


# --- ORM Models ---

class User(Base):
    """Table to store user information and roles."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String)
    hashed_password = Column(String, nullable=False)
    # 'administrator' or 'user'
    role = Column(String, nullable=False, default='user')
    is_active = Column(Boolean, default=True)
    # Many-to-many relationship with Workplace
    workplaces = relationship(
        "Workplace",
        secondary=workplace_user_association,
        back_populates="users")


class Bug(Base):
    """Table to store the bug IDs we are tracking."""
    __tablename__ = "bugs"
    id = Column(Integer, primary_key=True, index=True)
    bug_id = Column(Integer, unique=True, index=True, nullable=False)
    added_at = Column(DateTime(timezone=True), server_default=func.now())
    last_updated = Column(DateTime(timezone=True), onupdate=func.now())


class BugColumn(Base):
    """Defines the custom columns the user wants to track."""
    __tablename__ = "bug_columns"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    # Storing the Bugzilla field name, e.g., 'summary', 'status', 'cf_custom_field'
    bugzilla_field = Column(String, nullable=False)
    # The data type for our own reference and potential validation
    data_type = Column(String, nullable=False)  # 'bool', 'char', 'int', 'float'
    # --- NEW: Fields for UI control ---
    is_visible = Column(Boolean, default=True, nullable=False)
    is_static = Column(Boolean, default=False, nullable=False)
    # Many-to-many relationship with Workplace
    workplaces = relationship(
        "Workplace",
        secondary=workplace_column_association,
        back_populates="columns")


class BugHistory(Base):
    """Stores a snapshot of a bug's data at a specific time."""
    __tablename__ = "bug_history"
    id = Column(Integer, primary_key=True, index=True)
    bug_id = Column(Integer, ForeignKey("bugs.bug_id"), nullable=False, index=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    field_name = Column(String, nullable=False)
    field_value = Column(String)  # Store all values as strings and cast on the client

    __table_args__ = (
        # This composite index is CRITICAL for the performance of get_workplace_view
        Index('ix_bughistory_bug_field_time', 'bug_id', 'field_name', 'fetched_at'),
    )


class Workplace(Base):
    """Stores user-defined workplaces for grouping queries."""
    __tablename__ = "workplaces"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    # Many-to-many relationship with User
    users = relationship(
        "User",
        secondary=workplace_user_association,
        back_populates="workplaces")
    # Many-to-many relationship with BugColumn
    columns = relationship(
        "BugColumn",
        secondary=workplace_column_association,
        back_populates="workplaces")


class Query(Base):
    """Stores custom Bugzilla search queries and their schedule."""
    __tablename__ = "queries"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    query_url = Column(String, nullable=False)
    details = Column(String)
    workplace_id = Column(Integer, ForeignKey("workplaces.id"), nullable=True)

    # Scheduling Fields
    frequency_type = Column(String, default='manual')  # 'manual' or 'automatic'
    run_hour = Column(Integer)  # 0-23
    run_timezone = Column(String)  # e.g., 'UTC', 'America/New_York'
    frequency_interval_hours = Column(Float)  # e.g., 1, 7.5, 24

    # --- NEW: Execution Tracking Fields ---
    last_executed_at = Column(DateTime(timezone=True), nullable=True)
    next_execution_at = Column(DateTime(timezone=True), nullable=True)

    workplace = relationship("Workplace")


class ServiceStatus(Base):
    """Stores the last heartbeat of background services."""
    __tablename__ = "service_status"
    service_name = Column(String, primary_key=True)
    last_heartbeat = Column(DateTime(timezone=True), default=func.now())
    status = Column(String, default='offline')


class ExecutionLog(Base):
    """Stores a log for each individual query execution."""
    __tablename__ = "execution_logs"
    id = Column(Integer, primary_key=True, index=True)
    query_id = Column(Integer, ForeignKey("queries.id"), nullable=False)
    executed_at = Column(DateTime(timezone=True), server_default=func.now())
    total_bugs_processed = Column(Integer, default=0)
    new_bugs_added = Column(Integer, default=0)  # Bugs not seen before
    existing_bugs_updated = Column(Integer, default=0)  # Bugs that were already in the DB


class History(Base):
    """Stores a log of all data-modifying actions."""
    __tablename__ = "history"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User")
    action = Column(String, nullable=False)
    details = Column(String)


# --- Utility to create the database ---
def create_db_and_tables():
    Base.metadata.create_all(bind=engine)

    # --- Create Default Workplace ---
    db = SessionLocal()
    try:
        default_workplace = db.query(Workplace).filter(Workplace.name == "My Dashboard").first()
        if not default_workplace:
            print("Creating default 'My Dashboard' workplace.")
            db_workplace = Workplace(name="My Dashboard")
            db.add(db_workplace)
            db.commit()

        # --- Create Default Bug Columns ---
        column_count = db.query(BugColumn).count()
        if column_count == 0:
            print("Creating default bug columns.")
            default_columns = [
                # "Bug ID" is now implicit. "Summary" is the first static column.
                BugColumn(name="Summary", bugzilla_field="summary", data_type="char", is_static=True, is_visible=True),
                BugColumn(name="Status", bugzilla_field="status", data_type="char", is_static=False, is_visible=True),
                BugColumn(name="Resolution", bugzilla_field="resolution", data_type="char", is_static=False,
                          is_visible=True),
                BugColumn(name="Product", bugzilla_field="product", data_type="char", is_static=False, is_visible=True),
                BugColumn(name="Component", bugzilla_field="component", data_type="char", is_static=False,
                          is_visible=True),
                BugColumn(name="Assignee", bugzilla_field="assigned_to", data_type="char", is_static=False,
                          is_visible=True),
                BugColumn(name="Last Modified", bugzilla_field="last_change_time", data_type="char", is_static=False,
                          is_visible=False),
            ]
            db.add_all(default_columns)
            db.commit()
    finally:
        db.close()