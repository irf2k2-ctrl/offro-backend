"""Create the FIRST Super Admin on an empty database (the app no longer seeds a default account).

    python tools/create_super_admin.py [--mobile 9XXXXXXXXX]

Run it deliberately, against the database you mean (it reads MONGO_URL like the app). It asks for the PIN twice without echoing it, shows only the
database NAME, and asks you to type that name to confirm. The PIN is never printed, logged or stored in plain text (same PBKDF2 hash as the app).
It refuses when the mobile already exists, so it can never overwrite or reset an existing account. Sign-in still requires the SMS OTP.
"""
import argparse
import getpass
import sys
from datetime import datetime


def create_first_super_admin(db, mobile: str, pin: str, hash_pin) -> str:
    """Insert one Super Admin dashboard user. Returns the new id as a string. Raises ValueError with a safe message on bad input."""
    mobile, pin = str(mobile or "").strip(), str(pin or "")
    if not (mobile.isdigit() and len(mobile) == 10):
        raise ValueError("Mobile must be exactly 10 digits.")
    if not (pin.isdigit() and len(pin) in (4, 6)):
        raise ValueError("PIN must be 4 or 6 digits.")
    role = db.dashboard_roles.find_one({"role_name": "Super Admin"})
    if not role:
        raise ValueError("The Super Admin role does not exist yet. Start the application once so it seeds the default roles, then retry.")
    if db.dashboard_users.find_one({"mobile": mobile}):
        raise ValueError("A dashboard user with this mobile already exists; nothing was changed.")
    now = datetime.utcnow()
    res = db.dashboard_users.insert_one({
        "full_name": "Super Admin", "mobile": mobile, "pin": hash_pin(pin), "email": None, "designation": "Super Administrator",
        "profile_photo_url": None, "role_id": role["_id"], "assigned_cities": ["*"], "status": "active", "login_attempts": 0,
        "lockout_until": None, "last_login_at": None, "last_active_at": None, "token": None, "created_at": now, "updated_at": now})
    return str(res.inserted_id)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mobile", help="10-digit mobile number (prompted if omitted)")
    args = ap.parse_args(argv)
    from database import db                                   # imported late: reads MONGO_URL, exactly like the application
    from routers.dashboard_auth import hash_pin
    print("Database: " + db.name)
    if input("Type the database name above to continue: ").strip() != db.name:
        print("Cancelled; nothing was changed.")
        return 1
    mobile = args.mobile or input("Super Admin mobile (10 digits): ").strip()
    pin = getpass.getpass("PIN (4 or 6 digits, not shown): ")
    if pin != getpass.getpass("Repeat PIN: "):
        print("PINs do not match; nothing was changed.")
        return 1
    try:
        create_first_super_admin(db, mobile, pin, hash_pin)
    except ValueError as e:
        print(str(e))
        return 1
    print("Super Admin created for mobile ending " + mobile[-4:] + ". Sign in at /admin (PIN, then SMS code).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
