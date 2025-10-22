# backend/history.py
from sqlalchemy.orm import Session
from .database import History

def log_action(db: Session, user_id: int, action: str, details: str = None):
    """
    Logs an action to the history table.
    """
    history_entry = History(
        user_id=user_id,
        action=action,
        details=details
    )
    db.add(history_entry)
    db.commit()
