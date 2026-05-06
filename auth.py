from fastapi import HTTPException, Request
from database import db

def get_current_merchant(request: Request):

    # Retrieve token from cookies
    token = request.cookies.get("token")

    if not token:
        # If no token in cookies, check Authorization header
        auth_header = request.headers.get("Authorization")
        if auth_header and "Bearer " in auth_header:
            token = auth_header.split(" ")[1]

    if not token:
        raise HTTPException(status_code=401, detail="No token found")

    # Fetch merchant from DB using the token
    merchant = db.merchants.find_one({"token": token})

    if not merchant:
        raise HTTPException(status_code=403, detail="Invalid session")

    return merchant
