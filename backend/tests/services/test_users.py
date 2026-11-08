from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services import passwords, users


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(users, "db_path", lambda: tmp_path / "users.sqlite")
    monkeypatch.setattr(passwords, "N", 2**10)  # fast hashes in tests
    monkeypatch.setenv("VOITTA_OWNERS", "Boss@Example.com")
    users.reset_for_tests()
    yield
    users.reset_for_tests()


def test_invite_statuses_and_normalisation():
    out = users.invite("Ana@X.com, bo@y.org;ana@x.com  not-an-email", role="user", password="314159", actor="boss@example.com")
    assert out == [
        {"email": "ana@x.com", "status": "invited"},
        {"email": "bo@y.org", "status": "invited"},
        {"email": "not-an-email", "status": "invalid"},
    ]
    assert users.invite(["ana@x.com"]) == [{"email": "ana@x.com", "status": "exists"}]


def test_password_and_google_both_work_for_the_same_email():
    users.invite(["ana@x.com"], password="314159")
    u, why = users.password_login("ANA@x.com", "314159")
    assert u and why == ""
    g, why = users.admit_google("ana@x.com", "sub-1")
    assert g and why == "" and g["google_sub"] == "sub-1"
    # password still works after Google bound the account
    assert users.password_login("ana@x.com", "314159")[0] is not None


def test_unknown_and_wrong_password_look_the_same():
    users.invite(["ana@x.com"], password="right")
    assert users.password_login("ana@x.com", "nope") == (None, "wrong")
    assert users.password_login("ghost@x.com", "nope") == (None, "wrong")


def test_lockout_after_8_wrong_passwords():
    users.invite(["ana@x.com"], password="right")
    for _ in range(users.MAX_FAILS):
        assert users.password_login("ana@x.com", "bad")[1] == "wrong"
    assert users.password_login("ana@x.com", "right") == (None, "locked")
    assert users.lock_minutes("ana@x.com") == 15
    users.unlock("ana@x.com")
    assert users.password_login("ana@x.com", "right")[0] is not None


def test_not_invited_and_owner_autocreate_and_mismatch():
    assert users.admit_google("stranger@x.com", "s") == (None, "not_invited")
    boss, why = users.admit_google("boss@example.com", "boss-sub")
    assert boss and users.role_of(boss) == "owner"
    users.invite(["ana@x.com"])
    users.admit_google("ana@x.com", "sub-a")
    assert users.admit_google("ana@x.com", "other-sub") == (None, "mismatch")


def test_disable_cuts_sessions_and_keeps_user_listed_last():
    users.invite(["ana@x.com", "zed@x.com"], password="pw")
    ana = users.get("ana@x.com")
    sid = users.new_session(ana, "password")
    assert users.session_ok("ana@x.com", sid)
    users.disable("ana@x.com", actor="boss@example.com")
    assert not users.session_ok("ana@x.com", sid)
    assert users.password_login("ana@x.com", "pw") == (None, "disabled")
    assert users.admit_google("ana@x.com", "s") == (None, "disabled")
    assert [u["email"] for u in users.list_users()] == ["zed@x.com", "ana@x.com"]
    users.enable("ana@x.com", actor="boss@example.com")
    assert users.password_login("ana@x.com", "pw")[0] is not None
    assert not users.session_ok("ana@x.com", sid)  # old session stays dead


def test_owner_is_protected():
    users.admit_google("boss@example.com", "b")
    with pytest.raises(users.UserError):
        users.disable("boss@example.com", actor="x@x.com")
    with pytest.raises(users.UserError):
        users.set_role("boss@example.com", "user")


def test_set_password_ends_other_sessions_but_keeps_current():
    users.invite(["ana@x.com"], password="a")
    ana = users.get("ana@x.com")
    s1, s2 = users.new_session(ana, "password"), users.new_session(ana, "google")
    users.set_password("ana@x.com", "b", keep_sid=s1)
    assert users.session_ok("ana@x.com", s1) and not users.session_ok("ana@x.com", s2)
    assert users.password_login("ana@x.com", "b")[0] is not None


def test_session_must_match_email():
    users.invite(["ana@x.com", "bo@x.com"])
    sid = users.new_session(users.get("ana@x.com"), "google")
    assert not users.session_ok("bo@x.com", sid)


def test_sites_per_owner_and_normalisation():
    assert users.normalize_origin("https://App.StockTrak.com:443/x?y") == "https://app.stocktrak.com"
    assert users.normalize_origin("http://127.0.0.1:12358") == "http://127.0.0.1:12358"
    assert users.normalize_origin("null") is None
    assert users.normalize_origin("javascript:alert(1)") is None
    users.approve_site("ana@x.com", "https://app.stocktrak.com/")
    assert users.site_approved("ana@x.com", "https://app.stocktrak.com")
    assert not users.site_approved("bo@x.com", "https://app.stocktrak.com")
    users.remove_site("ana@x.com", "https://app.stocktrak.com")
    assert not users.site_approved("ana@x.com", "https://app.stocktrak.com")


def test_activity_records_admin_actions():
    users.invite(["ana@x.com"], actor="boss@example.com")
    users.disable("ana@x.com", actor="boss@example.com")
    acts = [a["action"] for a in users.activity()]
    assert acts[:2] == ["user.disable", "user.invite"]
