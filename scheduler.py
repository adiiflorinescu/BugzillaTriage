import os
import sys
import time
from datetime import datetime, timedelta

# Add the project root to the Python path to allow imports from the 'backend' package
# This is crucial for running this script as a standalone service.
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.append(project_root)

# Now we can import our application's components
from Bugzillatriage.backend.database import SessionLocal, Query
from Bugzillatriage.backend.main import perform_single_query_fetch

# --- Scheduler Configuration ---
# How often the scheduler wakes up to check for due tasks (in seconds).
# 60 seconds is a sensible default for a production environment.
SLEEP_INTERVAL_SECONDS = 60


def calculate_next_run(last_run_time: datetime, interval_hours: int) -> datetime:
    """Calculates the next execution time based on the last run and interval."""
    return last_run_time + timedelta(hours=interval_hours)


def run_scheduler():
    """
    The main loop of the scheduler service.
    This function runs indefinitely, checking for and executing due queries.
    """
    print("--- Scheduler Service Started ---")
    print(f"Checking for due queries every {SLEEP_INTERVAL_SECONDS} seconds.")

    while True:
        db = SessionLocal()
        try:
            now_utc = datetime.utcnow()
            print(f"[{now_utc.isoformat()}] Scheduler waking up...")

            # Find all automatic queries that are due to be run.
            # A query is due if its next_execution_at is in the past, or if it has never been run.
            due_queries = db.query(Query).filter(
                Query.frequency_type == 'automatic',
                (Query.next_execution_at <= now_utc) | (Query.next_execution_at == None)
            ).all()

            if not due_queries:
                print("No due queries found. Sleeping...")
            else:
                print(f"Found {len(due_queries)} due queries to execute.")
                for query in due_queries:
                    print(f"Executing query: '{query.name}' (ID: {query.id})")

                    # --- Execute the Query ---
                    # We use a new DB session for the background task as recommended by FastAPI patterns.
                    perform_single_query_fetch(query.id, SessionLocal())

                    # --- Update Timestamps for the Next Run ---
                    # We need to re-fetch the query object in the current session to update it.
                    query_to_update = db.query(Query).filter(Query.id == query.id).first()
                    if query_to_update:
                        current_time = datetime.utcnow()
                        query_to_update.last_executed_at = current_time

                        # Calculate and set the next execution time
                        if query_to_update.frequency_interval_hours:
                            next_run_time = calculate_next_run(current_time, query_to_update.frequency_interval_hours)
                            query_to_update.next_execution_at = next_run_time
                            print(f"Scheduled next run for query '{query.name}' at: {next_run_time.isoformat()}")
                        else:
                            # If for some reason there's no interval, prevent it from running again immediately
                            query_to_update.next_execution_at = current_time + timedelta(days=999)

                        db.commit()

        except Exception as e:
            # If any error occurs (e.g., database connection), log it and continue.
            # This makes the scheduler resilient.
            print(f"!!! An error occurred in the scheduler loop: {e}")
        finally:
            db.close()

        # Wait for the next cycle
        time.sleep(SLEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_scheduler()