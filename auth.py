from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

SECRET_KEY = "localsaver_secret"
ALGORITHM = "HS256"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

security = HTTPBearer()


# ----------------------------
# Password functions
# ----------------------------

def hash_password(password: str):
    return pwd_context.hash(password)


def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)


# ----------------------------
# JWT Token creation
# ----------------------------

def create_access_token(data: dict):

    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=7)

    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return encoded_jwt


# ----------------------------
# Decode JWT token
# ----------------------------

def decode_access_token(token: str):

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload

    except:
        return None


# ----------------------------
# Get current admin
# ----------------------------

def get_current_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):

    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return payload


# ----------------------------
# Get current merchant
# ----------------------------

def get_current_merchant(credentials: HTTPAuthorizationCredentials = Depends(security)):

    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None or "merchant_id" not in payload:
        raise HTTPException(status_code=403, detail="Merchant access required")

    return payload