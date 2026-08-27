"""
auth.py
=======
Session-based login/logout + role-based access control.

Roles (see db.ROLES): admin, sales, purchase, accountant.
    admin       - everything, including user management
    sales       - dashboard (receivables) + sales bills (create/edit)
    purchase    - purchase portal (stock-in from vendors)
    accountant  - read-only dashboard + can record payments + wallet entries

No users exist on a fresh install -> the app redirects everyone to /setup,
which only works while the users table is empty, to create the first admin
account without a hardcoded password anywhere in the code.
"""
from functools import wraps

from flask import session, redirect, url_for, request, flash, g

import db


def current_user():
    if "user_id" not in session:
        return None
    if not hasattr(g, "_current_user"):
        g._current_user = db.get_user(session["user_id"])
    return g._current_user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if db.any_users_exist() is False:
            return redirect(url_for("setup"))
        user = current_user()
        if not user or not user["active"]:
            session.clear()
            flash("Please log in to continue.")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please log in to continue.")
                return redirect(url_for("login", next=request.path))
            if user["role"] not in allowed_roles and user["role"] != "admin":
                flash("You don't have permission to access that page.")
                return redirect(url_for("home"))
            return view(*args, **kwargs)
        return wrapped
    return decorator
