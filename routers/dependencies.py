from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

# 🔐 SAME SECRET (VERY IMPORTANT)
SECRET = "localsaver_super_secret_key_1234567890_secure"

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        return payload
    except:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------- ROLE CHECKS ---------------- #

def admin_required(user=Depends(verify_token)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def merchant_required(user=Depends(verify_token)):
    if user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access required")
    return user


def user_required(user=Depends(verify_token)):
    if user.get("role") != "user":
        raise HTTPException(status_code=403, detail="User access required")
    return user