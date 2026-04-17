from services import auth_service
from database import get_engine
from sqlalchemy import text

EMAIL = "sortviewsuper@yourdomain.com"
PASSWORD = "ChangeThisRightNow123!"
FULL_NAME = "SortView Super Admin"

existing = auth_service.get_user_by_email(EMAIL)

if existing:
    user_id = existing["id"]
    print(f"User already exists: {EMAIL} (id={user_id})")
else:
    user = auth_service.create_user(
        email=EMAIL,
        password=PASSWORD,
        full_name=FULL_NAME,
    )
    user_id = user["id"]
    print(f"Created user: {EMAIL} (id={user_id})")

engine = get_engine()
with engine.begin() as conn:
    conn.execute(
        text("""
            update app_users
            set is_platform_admin = true
            where id = :user_id
        """),
        {"user_id": user_id},
    )

print("Super admin flag applied.")
