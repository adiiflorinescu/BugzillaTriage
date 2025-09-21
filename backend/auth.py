# backend/auth.py
from fastapi import Depends, HTTPException, status, Cookie
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from typing import Optional
from datetime import datetime, timedelta

# Import the centralized settings
from .config import settings
from .database import SessionLocal, User

def get_db():
    """Dependency to get a DB session for authentication checks."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Password Hashing ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Authentication Schemes ---
# For API calls using the 'Authorization: Bearer <token>' header
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/token", auto_error=False)

# For page navigation, reads the 'access_token' cookie
async def get_token_from_cookie(access_token: Optional[str] = Cookie(None)) -> Optional[str]:
    """
    Dependency to extract the token string from the 'access_token' cookie.
    """
    if access_token is None:
        return None
    # The cookie value is "Bearer <token>", so we split and take the token part
    parts = access_token.split()
    if len(parts) == 2 and parts[0] == "Bearer":
        return parts[1]
    return None


def verify_password(plain_password, hashed_password):
    """Verifies a plain password against a hashed one."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    """Hashes a plain password."""
    return pwd_context.hash(password)


def create_access_token(data: dict):
    """Creates a new JWT access token."""
    to_encode = data.copy()
    # Set token expiration
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    to_encode.update({"exp": expire})
    # Use settings for key and algorithm
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)
    return encoded_jwt


def get_user(db: Session, username: str) -> Optional[User]:
    """Fetches a user from the database by username."""
    return db.query(User).filter(User.username == username).first()


# --- Main Dependency to get the current user ---
async def get_current_user(
    token_from_header: Optional[str] = Depends(oauth2_scheme),
    token_from_cookie: Optional[str] = Depends(get_token_from_cookie),
    db: Session = Depends(get_db)
):
    """
    Dependency to decode JWT and get the current user from either the
    'Authorization' header or the 'access_token' cookie.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Prioritize the header for stateless API calls, but fall back to the cookie for page loads
    token = token_from_header or token_from_cookie

    if token is None:
        raise credentials_exception

    try:
        # Use settings for key and algorithm during decoding
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user(db, username=username)
    if user is None:
        raise credentials_exception
    return user


# --- Dependency for Administrator-only endpoints ---
async def get_current_admin_user(current_user: User = Depends(get_current_user)):
    """
    A dependency that builds on get_current_user.
    It ensures the user is also an administrator.
    """
    if current_user.role != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user does not have administrator privileges",
        )
    return current_user