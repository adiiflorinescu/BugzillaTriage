# backend/database.py
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Float, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func

# Import the centralized settings
from .config import settings

# --- Database Setup ---
# Use the database_url from the settings file
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    data_type = Column(String, nullable=False) # 'bool', 'char', 'int', 'float'

class BugHistory(Base):
    """Stores a snapshot of a bug's data at a specific time."""
    __tablename__ = "bug_history"
    id = Column(Integer, primary_key=True, index=True)
    bug_id = Column(Integer, ForeignKey("bugs.bug_id"), nullable=False, index=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    field_name = Column(String, nullable=False)
    field_value = Column(String) # Store all values as strings and cast on the client

class Workplace(Base):
    """Stores user-defined workplaces for grouping queries."""
    __tablename__ = "workplaces"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)

class Query(Base):
    """Stores custom Bugzilla search queries and their schedule."""
    __tablename__ = "queries"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    query_url = Column(String, nullable=False)
    details = Column(String)
    workplace_id = Column(Integer, ForeignKey("workplaces.id"), nullable=True)

    # Scheduling Fields
    frequency_type = Column(String, default='manual') # 'manual' or 'automatic'
    run_hour = Column(Integer) # 0-23
    run_timezone = Column(String) # e.g., 'UTC', 'America/New_York'
    frequency_interval_hours = Column(Integer) # e.g., 1, 24, 48

    # --- NEW: Execution Tracking Fields ---
    last_executed_at = Column(DateTime(timezone=True), nullable=True)
    next_execution_at = Column(DateTime(timezone=True), nullable=True)

    workplace = relationship("Workplace")


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
    finally:
        db.close()