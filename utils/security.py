from jose import jwt, JWTError
from datetime import datetime, timedelta
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# 🔐 Config
SECRET_KEY = "localsaver_secret_key"
ALGORITHM = "HS256"

# =========================
# 🔹 QR TOKEN FUNCTIONS
# =========================

def create_qr_token(user_id: str):
    expire = datetime.utcnow() + timedelta(seconds=60)  # QR valid for 60 seconds

    payload = {
        "user_id": user_id,
        "exp": expire
    }

    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token


def verify_qr_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["user_id"]
    except JWTError:
        return None


# =========================
# 🔹 MERCHANT AUTH (JWT)
# =========================

security = HTTPBearer()


def get_current_merchant(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        merchant_id = payload.get("merchant_id")

        if not merchant_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        return merchant_id

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")