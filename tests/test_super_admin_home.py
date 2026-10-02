from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPER_ADMIN_HOME = ROOT / "src" / "pages" / "Super_Admin_Home.py"


def test_super_admin_home_enforces_active_session_before_platform_admin_check():
    source = SUPER_ADMIN_HOME.read_text(encoding="utf-8")

    assert "from services import auth_service" in source

    enforce = 'auth_service.enforce_active_session(auth_user)'
    platform_check = 'is_platform_admin(auth_user["id"])'

    assert enforce in source
    assert platform_check in source
    assert source.index(enforce) < source.index(platform_check)
