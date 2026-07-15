from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from .camt import parse_camt
from .db import close_db, get_db, init_db, log_action


ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
TAX_AREAS = (
    "Ideeller Bereich",
    "Vermögensverwaltung",
    "Zweckbetrieb",
    "Wirtschaftlicher Geschäftsbetrieb",
)
ORGANIZATION_ID = 1


def parse_amount_cents(value):
    normalized = (value or "").strip().replace(" ", "")
    if not normalized:
        raise ValueError("Betrag fehlt")
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(normalized).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Betrag ist ungültig") from exc
    return int(amount * 100)


def create_app(test_config=None):
    app = Flask(__name__)
    data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin"),
        DATA_DIR=data_dir,
        DATABASE=str(data_dir / "vereinskasse.sqlite3"),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)

    for folder in ("imports", "attachments"):
        (Path(app.config["DATA_DIR"]) / folder).mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()

    @app.template_filter("money")
    def money(cents, currency="EUR"):
        amount = (cents or 0) / 100
        formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{formatted} {currency}"

    @app.template_filter("date_de")
    def date_de(value):
        try:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            return value or ""

    def login_required(view):
        @wraps(view)
        def wrapped(**kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.path))
            return view(**kwargs)

        return wrapped

    @app.before_request
    def csrf_protect():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "login":
            token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not token or not hmac.compare_digest(token, session.get("csrf_token", "")):
                abort(400, "Ungültiges CSRF-Token")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.endpoint in {"review_public", "review_attachment"}:
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.context_processor
    def globals_for_templates():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        organization = get_db().execute(
            "SELECT * FROM organizations WHERE id=?", (ORGANIZATION_ID,)
        ).fetchone()
        return {
            "csrf_token": session["csrf_token"],
            "tax_areas": TAX_AREAS,
            "organization": organization,
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if hmac.compare_digest(password, app.config["ADMIN_PASSWORD"]):
                session.clear()
                session["authenticated"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                target = request.args.get("next", "")
                if not target.startswith("/") or target.startswith("//"):
                    target = url_for("dashboard")
                return redirect(target)
            flash("Das Passwort ist nicht korrekt.", "error")
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        db = get_db()
        selected_year = request.args.get("year", str(datetime.now().year))
        years = [
            row["year"]
            for row in db.execute(
                "SELECT DISTINCT substr(booking_date, 1, 4) AS year FROM transactions ORDER BY year DESC"
            )
        ]
        if selected_year not in years:
            years.insert(0, selected_year)
        totals = db.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) income,
              COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) expenses,
              COALESCE(SUM(amount_cents), 0) balance,
              COUNT(*) count,
              SUM(CASE WHEN receipt_status = 'missing' THEN 1 ELSE 0 END) missing,
              SUM(CASE WHEN category_id IS NULL THEN 1 ELSE 0 END) uncategorized
            FROM transactions WHERE substr(booking_date, 1, 4) = ?
            """,
            (selected_year,),
        ).fetchone()
        by_category = db.execute(
            """
            SELECT COALESCE(c.name, 'Ohne Kategorie') name,
                   COALESCE(c.tax_area, 'Nicht zugeordnet') tax_area,
                   SUM(t.amount_cents) total, COUNT(*) count
            FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
            WHERE substr(t.booking_date, 1, 4) = ?
            GROUP BY c.id, c.name, c.tax_area ORDER BY ABS(SUM(t.amount_cents)) DESC
            """,
            (selected_year,),
        ).fetchall()
        recent = db.execute(
            """
            SELECT t.*, c.name category_name, a.name account_name, a.kind account_kind,
                   (SELECT COUNT(*) FROM attachments a WHERE a.transaction_id=t.id) attachment_count
            FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
            LEFT JOIN accounts a ON a.id=t.account_id
            ORDER BY booking_date DESC, t.id DESC LIMIT 8
            """
        ).fetchall()
        account_balances = db.execute(
            """
            SELECT a.*, a.opening_balance_cents + COALESCE(SUM(t.amount_cents), 0) balance_cents,
                   COUNT(t.id) transaction_count
            FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id
            WHERE a.organization_id=? AND a.active=1
            GROUP BY a.id ORDER BY a.kind, a.name
            """,
            (ORGANIZATION_ID,),
        ).fetchall()
        return render_template(
            "dashboard.html",
            totals=totals,
            by_category=by_category,
            recent=recent,
            years=years,
            selected_year=selected_year,
            account_balances=account_balances,
        )

    @app.get("/transactions")
    @login_required
    def transactions():
        db = get_db()
        year = request.args.get("year", "")
        status = request.args.get("status", "")
        account_id = request.args.get("account_id", "")
        query = """
            SELECT t.*, c.name category_name, a.name account_name, a.kind account_kind,
                   (SELECT COUNT(*) FROM attachments a WHERE a.transaction_id=t.id) attachment_count
            FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
            LEFT JOIN accounts a ON a.id=t.account_id WHERE 1=1
        """
        params = []
        if year:
            query += " AND substr(t.booking_date,1,4)=?"
            params.append(year)
        if status == "missing":
            query += " AND t.receipt_status='missing'"
        elif status == "uncategorized":
            query += " AND t.category_id IS NULL"
        if account_id:
            query += " AND t.account_id=?"
            params.append(account_id)
        query += " ORDER BY t.booking_date DESC, t.id DESC"
        rows = db.execute(query, params).fetchall()
        years = db.execute(
            "SELECT DISTINCT substr(booking_date,1,4) year FROM transactions ORDER BY year DESC"
        ).fetchall()
        accounts = db.execute(
            "SELECT * FROM accounts WHERE organization_id=? AND active=1 ORDER BY kind,name",
            (ORGANIZATION_ID,),
        ).fetchall()
        return render_template(
            "transactions.html",
            transactions=rows,
            years=years,
            year=year,
            status=status,
            accounts=accounts,
            account_id=account_id,
        )

    @app.get("/transactions/<int:transaction_id>")
    @login_required
    def transaction_detail(transaction_id):
        db = get_db()
        transaction = db.execute(
            """SELECT t.*, c.name category_name, c.tax_area,
                      a.name account_name, a.kind account_kind
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
               LEFT JOIN accounts a ON a.id=t.account_id WHERE t.id=?""",
            (transaction_id,),
        ).fetchone()
        if transaction is None:
            abort(404)
        attachments = db.execute(
            "SELECT * FROM attachments WHERE transaction_id=? ORDER BY created_at", (transaction_id,)
        ).fetchall()
        categories = db.execute("SELECT * FROM categories WHERE active=1 ORDER BY name").fetchall()
        return render_template(
            "transaction_detail.html",
            transaction=transaction,
            attachments=attachments,
            categories=categories,
        )

    @app.post("/transactions/<int:transaction_id>/update")
    @login_required
    def transaction_update(transaction_id):
        db = get_db()
        category_id = request.form.get("category_id") or None
        status = request.form.get("receipt_status", "missing")
        if status not in {"missing", "complete", "not_required"}:
            abort(400)
        db.execute(
            """UPDATE transactions SET category_id=?, receipt_status=?, note=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (category_id, status, request.form.get("note", "").strip(), transaction_id),
        )
        log_action(
            db,
            "updated",
            "transaction",
            transaction_id,
            json.dumps({"category_id": category_id, "receipt_status": status}, ensure_ascii=False),
        )
        db.commit()
        flash("Buchung gespeichert.", "success")
        return redirect(url_for("transaction_detail", transaction_id=transaction_id))

    @app.post("/transactions/<int:transaction_id>/attachments")
    @login_required
    def attachment_upload(transaction_id):
        upload = request.files.get("attachment")
        if not upload or not upload.filename:
            flash("Bitte eine Datei auswählen.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        extension = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if extension not in ALLOWED_EXTENSIONS:
            flash("Erlaubt sind PDF-, PNG- und JPEG-Dateien.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))

        db = get_db()
        if db.execute("SELECT 1 FROM transactions WHERE id=?", (transaction_id,)).fetchone() is None:
            abort(404)
        storage_name = f"{uuid.uuid4().hex}.{extension}"
        target = Path(app.config["DATA_DIR"]) / "attachments" / storage_name
        upload.save(target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        db.execute(
            """INSERT INTO attachments(transaction_id, original_name, stored_path, mime_type, file_hash, size_bytes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                transaction_id,
                secure_filename(upload.filename) or f"beleg.{extension}",
                str(target.relative_to(app.config["DATA_DIR"])),
                upload.mimetype,
                digest,
                target.stat().st_size,
            ),
        )
        db.execute(
            "UPDATE transactions SET receipt_status='complete', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (transaction_id,),
        )
        log_action(db, "uploaded", "attachment", transaction_id, upload.filename)
        db.commit()
        flash("Beleg hochgeladen.", "success")
        return redirect(url_for("transaction_detail", transaction_id=transaction_id))

    @app.get("/attachments/<int:attachment_id>")
    @login_required
    def attachment_download(attachment_id):
        row = get_db().execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if row is None:
            abort(404)
        path = Path(app.config["DATA_DIR"]) / row["stored_path"]
        return send_file(path, download_name=row["original_name"], as_attachment=False)

    @app.route("/accounts", methods=["GET", "POST"])
    @login_required
    def accounts():
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "create")
            if action == "organization":
                name = request.form.get("organization_name", "").strip()
                if not name:
                    flash("Bitte einen Vereinsnamen angeben.", "error")
                else:
                    db.execute("UPDATE organizations SET name=? WHERE id=?", (name, ORGANIZATION_ID))
                    log_action(db, "updated", "organization", ORGANIZATION_ID, name)
                    db.commit()
                    flash("Vereinsname gespeichert.", "success")
                return redirect(url_for("accounts"))

            kind = request.form.get("kind", "bank")
            name = request.form.get("name", "").strip()
            iban = "".join(request.form.get("iban", "").upper().split()) or None
            try:
                opening_balance = parse_amount_cents(request.form.get("opening_balance", "0"))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("accounts"))
            if kind not in {"bank", "cash"} or not name:
                flash("Bitte Kontoname und Kontotyp angeben.", "error")
                return redirect(url_for("accounts"))
            count = db.execute(
                "SELECT COUNT(*) FROM accounts WHERE organization_id=? AND kind=? AND active=1",
                (ORGANIZATION_ID, kind),
            ).fetchone()[0]
            limit = 3 if kind == "bank" else 1
            if count >= limit:
                label = "Bankkonten" if kind == "bank" else "Barkasse"
                flash(f"Es sind maximal {limit} aktive {label} möglich.", "error")
                return redirect(url_for("accounts"))
            try:
                cursor = db.execute(
                    """INSERT INTO accounts(organization_id,name,kind,iban,opening_balance_cents)
                       VALUES (?,?,?,?,?)""",
                    (ORGANIZATION_ID, name, kind, iban if kind == "bank" else None, opening_balance),
                )
            except sqlite3.IntegrityError:
                flash("Kontoname oder IBAN wird bereits verwendet.", "error")
                return redirect(url_for("accounts"))
            log_action(db, "created", "account", cursor.lastrowid, name)
            db.commit()
            flash("Konto angelegt.", "success")
            return redirect(url_for("accounts"))

        rows = db.execute(
            """SELECT a.*, a.opening_balance_cents + COALESCE(SUM(t.amount_cents),0) balance_cents,
                      COUNT(t.id) transaction_count
               FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id
               WHERE a.organization_id=? AND a.active=1
               GROUP BY a.id ORDER BY a.kind,a.name""",
            (ORGANIZATION_ID,),
        ).fetchall()
        categories = db.execute("SELECT * FROM categories WHERE active=1 ORDER BY name").fetchall()
        return render_template("accounts.html", accounts=rows, categories=categories)

    @app.post("/accounts/<int:account_id>/cash-entry")
    @login_required
    def cash_entry(account_id):
        db = get_db()
        account = db.execute(
            "SELECT * FROM accounts WHERE id=? AND organization_id=? AND kind='cash' AND active=1",
            (account_id, ORGANIZATION_ID),
        ).fetchone()
        if account is None:
            abort(404)
        try:
            amount = abs(parse_amount_cents(request.form.get("amount")))
            booking_date = datetime.fromisoformat(request.form.get("booking_date", "")).date().isoformat()
        except (ValueError, TypeError):
            flash("Bitte Datum und gültigen Betrag angeben.", "error")
            return redirect(url_for("accounts"))
        if request.form.get("direction") == "expense":
            amount = -amount
        purpose = request.form.get("purpose", "").strip()
        if not purpose:
            flash("Bitte einen Verwendungszweck angeben.", "error")
            return redirect(url_for("accounts"))
        receipt_status = request.form.get("receipt_status", "missing")
        if receipt_status not in {"missing", "complete", "not_required"}:
            abort(400)
        marker = uuid.uuid4().hex
        batch = db.execute(
            """INSERT INTO import_batches(account_id,filename,file_hash,stored_path,account_iban,imported_count)
               VALUES (?,?,?,?,?,1)""",
            (account_id, "Manuelle Barbuchung", f"manual:{marker}", "", None),
        )
        cursor = db.execute(
            """INSERT INTO transactions(
               account_id,import_batch_id,fingerprint,booking_date,value_date,amount_cents,currency,
               counterparty,purpose,category_id,receipt_status,note,bank_transaction_code
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account_id,
                batch.lastrowid,
                hashlib.sha256(f"cash:{marker}".encode()).hexdigest(),
                booking_date,
                booking_date,
                amount,
                account["currency"],
                request.form.get("counterparty", "").strip() or "Barbuchung",
                purpose,
                request.form.get("category_id") or None,
                receipt_status,
                request.form.get("note", "").strip(),
                "CASH",
            ),
        )
        log_action(db, "created", "cash_transaction", cursor.lastrowid, purpose)
        db.commit()
        flash("Barbuchung erfasst.", "success")
        return redirect(url_for("transaction_detail", transaction_id=cursor.lastrowid))

    @app.post("/accounts/<int:account_id>/update")
    @login_required
    def account_update(account_id):
        db = get_db()
        account = db.execute(
            "SELECT * FROM accounts WHERE id=? AND organization_id=? AND active=1",
            (account_id, ORGANIZATION_ID),
        ).fetchone()
        if account is None:
            abort(404)
        name = request.form.get("name", "").strip()
        iban = "".join(request.form.get("iban", "").upper().split()) or None
        if account["kind"] == "cash":
            iban = None
        try:
            opening_balance = parse_amount_cents(request.form.get("opening_balance", "0"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("accounts"))
        if not name:
            flash("Bitte einen Kontonamen angeben.", "error")
            return redirect(url_for("accounts"))
        try:
            db.execute(
                "UPDATE accounts SET name=?,iban=?,opening_balance_cents=? WHERE id=?",
                (name, iban, opening_balance, account_id),
            )
        except sqlite3.IntegrityError:
            flash("Kontoname oder IBAN wird bereits verwendet.", "error")
            return redirect(url_for("accounts"))
        log_action(
            db,
            "updated",
            "account",
            account_id,
            json.dumps({"name": name, "opening_balance_cents": opening_balance}),
        )
        db.commit()
        flash("Kontodaten gespeichert.", "success")
        return redirect(url_for("accounts"))

    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_file():
        if request.method == "GET":
            db = get_db()
            batches = db.execute(
                """SELECT b.*, a.name account_name FROM import_batches b
                   LEFT JOIN accounts a ON a.id=b.account_id
                   WHERE b.stored_path != '' ORDER BY b.created_at DESC LIMIT 20"""
            ).fetchall()
            accounts = db.execute(
                "SELECT * FROM accounts WHERE organization_id=? AND kind='bank' AND active=1 ORDER BY name",
                (ORGANIZATION_ID,),
            ).fetchall()
            return render_template("import.html", batches=batches, accounts=accounts)

        upload = request.files.get("statement")
        if not upload or not upload.filename:
            flash("Bitte eine CAMT-Datei auswählen.", "error")
            return redirect(url_for("import_file"))
        temporary = Path(app.config["DATA_DIR"]) / "imports" / f"tmp-{uuid.uuid4().hex}.xml"
        upload.save(temporary)
        file_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        db = get_db()
        if db.execute("SELECT 1 FROM import_batches WHERE file_hash=?", (file_hash,)).fetchone():
            temporary.unlink(missing_ok=True)
            flash("Diese CAMT-Datei wurde bereits importiert.", "error")
            return redirect(url_for("import_file"))
        try:
            report = parse_camt(temporary)
        except ValueError as exc:
            temporary.unlink(missing_ok=True)
            flash(str(exc), "error")
            return redirect(url_for("import_file"))

        selected_account_id = request.form.get("account_id")
        account = None
        if selected_account_id:
            account = db.execute(
                """SELECT * FROM accounts WHERE id=? AND organization_id=?
                   AND kind='bank' AND active=1""",
                (selected_account_id, ORGANIZATION_ID),
            ).fetchone()
            if account is None:
                temporary.unlink(missing_ok=True)
                abort(400, "Ungültiges Bankkonto")
            if account["iban"] and report.account_iban and account["iban"] != report.account_iban:
                temporary.unlink(missing_ok=True)
                flash("Die IBAN der Datei passt nicht zum ausgewählten Konto.", "error")
                return redirect(url_for("import_file"))
            if not account["iban"] and report.account_iban:
                db.execute("UPDATE accounts SET iban=? WHERE id=?", (report.account_iban, account["id"]))
        elif report.account_iban:
            account = db.execute(
                "SELECT * FROM accounts WHERE organization_id=? AND iban=?",
                (ORGANIZATION_ID, report.account_iban),
            ).fetchone()
        if account is None:
            bank_count = db.execute(
                "SELECT COUNT(*) FROM accounts WHERE organization_id=? AND kind='bank' AND active=1",
                (ORGANIZATION_ID,),
            ).fetchone()[0]
            if bank_count >= 3:
                temporary.unlink(missing_ok=True)
                flash("Bitte ein bestehendes Bankkonto auswählen; das Limit von drei Konten ist erreicht.", "error")
                return redirect(url_for("import_file"))
            suffix = (report.account_iban or "")[-4:]
            cursor = db.execute(
                """INSERT INTO accounts(organization_id,name,kind,iban)
                   VALUES (?,?,'bank',?)""",
                (ORGANIZATION_ID, f"Bankkonto {suffix}" if suffix else "Bankkonto", report.account_iban),
            )
            account = db.execute("SELECT * FROM accounts WHERE id=?", (cursor.lastrowid,)).fetchone()

        final_path = Path(app.config["DATA_DIR"]) / "imports" / f"{file_hash}.xml"
        shutil.move(temporary, final_path)
        cursor = db.execute(
            """INSERT INTO import_batches(account_id,filename,file_hash,stored_path,account_iban)
               VALUES (?,?,?,?,?)""",
            (
                account["id"],
                secure_filename(upload.filename) or "kontoauszug.xml",
                file_hash,
                str(final_path.relative_to(app.config["DATA_DIR"])),
                report.account_iban,
            ),
        )
        batch_id = cursor.lastrowid
        imported = 0
        duplicates = 0
        for tx in report.transactions:
            try:
                db.execute(
                    """INSERT INTO transactions(
                       account_id,import_batch_id,fingerprint,account_iban,booking_date,value_date,amount_cents,
                       currency,counterparty,counterparty_iban,purpose,bank_reference,bank_transaction_code
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        account["id"],
                        batch_id,
                        tx.fingerprint,
                        tx.account_iban,
                        tx.booking_date,
                        tx.value_date,
                        tx.amount_cents,
                        tx.currency,
                        tx.counterparty,
                        tx.counterparty_iban,
                        tx.purpose,
                        tx.bank_reference,
                        tx.bank_transaction_code,
                    ),
                )
                imported += 1
            except sqlite3.IntegrityError as exc:
                if "transactions.fingerprint" not in str(exc):
                    raise
                duplicates += 1
        db.execute(
            "UPDATE import_batches SET imported_count=?, duplicate_count=? WHERE id=?",
            (imported, duplicates, batch_id),
        )
        log_action(
            db,
            "imported",
            "import_batch",
            batch_id,
            json.dumps({"filename": upload.filename, "imported": imported, "duplicates": duplicates}),
        )
        db.commit()
        flash(f"Import abgeschlossen: {imported} neue Buchungen, {duplicates} Duplikate.", "success")
        return redirect(url_for("transactions", status="uncategorized"))

    @app.get("/imports/<int:batch_id>/original")
    @login_required
    def import_download(batch_id):
        row = get_db().execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            abort(404)
        return send_file(
            Path(app.config["DATA_DIR"]) / row["stored_path"],
            download_name=row["filename"],
            as_attachment=True,
        )

    @app.route("/categories", methods=["GET", "POST"])
    @login_required
    def categories():
        db = get_db()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            tax_area = request.form.get("tax_area", "")
            if not name or tax_area not in TAX_AREAS:
                flash("Bitte Name und Steuerbereich vollständig angeben.", "error")
            else:
                try:
                    cursor = db.execute(
                        "INSERT INTO categories(name,tax_area) VALUES (?,?)", (name, tax_area)
                    )
                    log_action(db, "created", "category", cursor.lastrowid, name)
                    db.commit()
                    flash("Kategorie angelegt.", "success")
                except Exception as exc:
                    if "UNIQUE constraint failed" in str(exc):
                        flash("Diese Kategorie existiert bereits.", "error")
                    else:
                        raise
            return redirect(url_for("categories"))
        rows = db.execute(
            """SELECT c.*, COUNT(t.id) transaction_count FROM categories c
               LEFT JOIN transactions t ON t.category_id=c.id GROUP BY c.id ORDER BY c.tax_area,c.name"""
        ).fetchall()
        return render_template("categories.html", categories=rows)

    @app.get("/audit")
    @login_required
    def audit():
        rows = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 250").fetchall()
        return render_template("audit.html", entries=rows)

    @app.route("/reviews", methods=["GET", "POST"])
    @login_required
    def reviews():
        db = get_db()
        if request.method == "POST":
            year = request.form.get("year", "").strip()
            if len(year) != 4 or not year.isdigit():
                flash("Bitte ein gültiges Geschäftsjahr auswählen.", "error")
                return redirect(url_for("reviews"))
            rows = db.execute(
                """SELECT t.*, c.name category_name, c.tax_area,
                          a.name account_name, a.kind account_kind
                   FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
                   LEFT JOIN accounts a ON a.id=t.account_id
                   WHERE substr(t.booking_date,1,4)=?
                   ORDER BY t.booking_date,t.id""",
                (year,),
            ).fetchall()
            snapshot_transactions = []
            for row in rows:
                item = dict(row)
                item["attachments"] = [
                    dict(attachment)
                    for attachment in db.execute(
                        """SELECT id,original_name,mime_type,file_hash,size_bytes,created_at
                           FROM attachments WHERE transaction_id=? ORDER BY id""",
                        (row["id"],),
                    ).fetchall()
                ]
                snapshot_transactions.append(item)
            accounts_snapshot = [
                dict(row)
                for row in db.execute(
                    """SELECT a.id,a.name,a.kind,a.iban,a.currency,a.opening_balance_cents,
                              a.opening_balance_cents + COALESCE(SUM(t.amount_cents),0) balance_cents
                       FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id
                       WHERE a.organization_id=? AND a.active=1
                       GROUP BY a.id ORDER BY a.kind,a.name""",
                    (ORGANIZATION_ID,),
                ).fetchall()
            ]
            organization = db.execute(
                "SELECT name FROM organizations WHERE id=?", (ORGANIZATION_ID,)
            ).fetchone()
            snapshot = {
                "organization_name": organization["name"],
                "year": year,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "accounts": accounts_snapshot,
                "transactions": snapshot_transactions,
            }
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            days = request.form.get("expires_days", "30")
            if days not in {"7", "30", "90"}:
                days = "30"
            expires_at = (datetime.now(timezone.utc) + timedelta(days=int(days))).isoformat()
            label = request.form.get("label", "").strip() or f"Kassenprüfung {year}"
            cursor = db.execute(
                """INSERT INTO review_shares(
                   organization_id,label,year,token_hash,snapshot_json,expires_at
                   ) VALUES (?,?,?,?,?,?)""",
                (ORGANIZATION_ID, label, year, token_hash, json.dumps(snapshot, ensure_ascii=False), expires_at),
            )
            log_action(db, "created", "review_share", cursor.lastrowid, label)
            db.commit()
            share_url = url_for("review_public", token=token, _external=True)
            return render_template(
                "review_created.html", share_url=share_url, label=label, expires_at=expires_at
            )

        years = [
            row["year"]
            for row in db.execute(
                "SELECT DISTINCT substr(booking_date,1,4) year FROM transactions ORDER BY year DESC"
            ).fetchall()
        ]
        shares = db.execute(
            "SELECT * FROM review_shares WHERE organization_id=? ORDER BY id DESC",
            (ORGANIZATION_ID,),
        ).fetchall()
        return render_template("reviews.html", shares=shares, years=years)

    @app.post("/reviews/<int:share_id>/revoke")
    @login_required
    def review_revoke(share_id):
        db = get_db()
        db.execute(
            """UPDATE review_shares SET revoked_at=CURRENT_TIMESTAMP
               WHERE id=? AND organization_id=? AND revoked_at IS NULL""",
            (share_id, ORGANIZATION_ID),
        )
        log_action(db, "revoked", "review_share", share_id)
        db.commit()
        flash("Prüfungslink widerrufen.", "success")
        return redirect(url_for("reviews"))

    def load_review_share(token):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        share = get_db().execute(
            "SELECT * FROM review_shares WHERE token_hash=? AND revoked_at IS NULL", (token_hash,)
        ).fetchone()
        if share is None:
            abort(404)
        if share["expires_at"] and datetime.fromisoformat(share["expires_at"]) <= datetime.now(timezone.utc):
            abort(410)
        return share, json.loads(share["snapshot_json"])

    @app.get("/review/<token>")
    def review_public(token):
        share, snapshot = load_review_share(token)
        transactions = snapshot["transactions"]
        totals = {
            "income": sum(row["amount_cents"] for row in transactions if row["amount_cents"] > 0),
            "expenses": -sum(row["amount_cents"] for row in transactions if row["amount_cents"] < 0),
            "balance": sum(row["amount_cents"] for row in transactions),
            "missing": sum(row["receipt_status"] == "missing" for row in transactions),
            "uncategorized": sum(row["category_id"] is None for row in transactions),
        }
        return render_template(
            "review_public.html", share=share, snapshot=snapshot, transactions=transactions, totals=totals, token=token
        )

    @app.get("/review/<token>/attachments/<int:attachment_id>")
    def review_attachment(token, attachment_id):
        _share, snapshot = load_review_share(token)
        allowed_ids = {
            attachment["id"]
            for transaction in snapshot["transactions"]
            for attachment in transaction["attachments"]
        }
        if attachment_id not in allowed_ids:
            abort(404)
        row = get_db().execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if row is None:
            abort(404)
        return send_file(
            Path(app.config["DATA_DIR"]) / row["stored_path"],
            download_name=row["original_name"],
            as_attachment=False,
        )

    @app.get("/export.csv")
    @login_required
    def export_csv():
        year = request.args.get("year", str(datetime.now().year))
        rows = get_db().execute(
            """SELECT t.*, c.name category_name, c.tax_area, a.name account_name,
                      (SELECT COUNT(*) FROM attachments a WHERE a.transaction_id=t.id) attachment_count
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
               LEFT JOIN accounts a ON a.id=t.account_id
               WHERE substr(t.booking_date,1,4)=? ORDER BY t.booking_date,t.id""",
            (year,),
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(
            ["Konto", "Buchungsdatum", "Wertstellung", "Betrag", "Währung", "Gegenpartei", "IBAN", "Zweck", "Kategorie", "Steuerbereich", "Belegstatus", "Belege", "Bankreferenz"]
        )
        labels = {"missing": "Fehlt", "complete": "Vollständig", "not_required": "Nicht erforderlich"}
        for row in rows:
            writer.writerow(
                [
                    row["account_name"], row["booking_date"], row["value_date"], f"{row['amount_cents']/100:.2f}".replace(".", ","),
                    row["currency"], row["counterparty"], row["counterparty_iban"], row["purpose"],
                    row["category_name"], row["tax_area"], labels[row["receipt_status"]],
                    row["attachment_count"], row["bank_reference"],
                ]
            )
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="vereinskasse-{year}.csv"'},
        )

    return app
