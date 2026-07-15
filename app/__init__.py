from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import uuid
import zipfile
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
from .csv_import import parse_csv, preview_csv
from .db import close_db, get_db, init_db, log_action
from .mt940 import parse_mt940


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

    def year_is_closed(db, year):
        return db.execute(
            "SELECT 1 FROM year_closures WHERE organization_id=? AND year=?",
            (ORGANIZATION_ID, str(year)),
        ).fetchone() is not None

    def transaction_is_closed(db, transaction_id):
        row = db.execute("SELECT booking_date FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        return row is not None and year_is_closed(db, row["booking_date"][:4])

    def transaction_is_adjustment(db, transaction_id):
        return db.execute(
            """SELECT 1 FROM transaction_adjustments
               WHERE original_transaction_id=? OR reversal_transaction_id=?
                  OR replacement_transaction_id=?""",
            (transaction_id, transaction_id, transaction_id),
        ).fetchone() is not None

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
                   SUM(b.amount_cents) total, COUNT(*) count
            FROM (
                SELECT t.category_id, t.amount_cents FROM transactions t
                WHERE substr(t.booking_date,1,4)=?
                  AND NOT EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)
                UNION ALL
                SELECT s.category_id, s.amount_cents FROM transaction_splits s
                JOIN transactions t ON t.id=s.transaction_id
                WHERE substr(t.booking_date,1,4)=?
            ) b LEFT JOIN categories c ON c.id = b.category_id
            GROUP BY c.id, c.name, c.tax_area ORDER BY ABS(SUM(b.amount_cents)) DESC
            """,
            (selected_year, selected_year),
        ).fetchall()
        recent = db.execute(
            """
            SELECT t.*, c.name category_name, a.name account_name, a.kind account_kind,
                   (SELECT COUNT(*) FROM transaction_splits s WHERE s.transaction_id=t.id) split_count,
                   (SELECT kind FROM transaction_adjustments x WHERE x.original_transaction_id=t.id) adjustment_kind,
                   CASE
                     WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.reversal_transaction_id=t.id) THEN 'reversal'
                     WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.replacement_transaction_id=t.id) THEN 'replacement'
                   END adjustment_role,
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
                   (SELECT COUNT(*) FROM transaction_splits s WHERE s.transaction_id=t.id) split_count,
                   (SELECT kind FROM transaction_adjustments x WHERE x.original_transaction_id=t.id) adjustment_kind,
                   CASE
                     WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.reversal_transaction_id=t.id) THEN 'reversal'
                     WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.replacement_transaction_id=t.id) THEN 'replacement'
                   END adjustment_role,
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
            query += " AND t.category_id IS NULL AND NOT EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id)"
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
        splits = db.execute(
            """SELECT s.*, c.name category_name, c.tax_area FROM transaction_splits s
               JOIN categories c ON c.id=s.category_id WHERE s.transaction_id=? ORDER BY s.id""",
            (transaction_id,),
        ).fetchall()
        adjustment = db.execute(
            """SELECT x.*, original.purpose original_purpose,
                      reversal.amount_cents reversal_amount,
                      replacement.amount_cents replacement_amount
               FROM transaction_adjustments x
               JOIN transactions original ON original.id=x.original_transaction_id
               JOIN transactions reversal ON reversal.id=x.reversal_transaction_id
               LEFT JOIN transactions replacement ON replacement.id=x.replacement_transaction_id
               WHERE x.original_transaction_id=? OR x.reversal_transaction_id=?
                  OR x.replacement_transaction_id=?""",
            (transaction_id, transaction_id, transaction_id),
        ).fetchone()
        return render_template(
            "transaction_detail.html",
            transaction=transaction,
            attachments=attachments,
            categories=categories,
            splits=splits,
            split_total=sum(row["amount_cents"] for row in splits),
            year_closed=year_is_closed(db, transaction["booking_date"][:4]),
            adjustment=adjustment,
            is_adjustment=transaction_is_adjustment(db, transaction_id),
            today=datetime.now().date().isoformat(),
        )

    @app.post("/transactions/<int:transaction_id>/update")
    @login_required
    def transaction_update(transaction_id):
        db = get_db()
        if transaction_is_adjustment(db, transaction_id):
            flash("Storno- und Korrekturbuchungen sind unveränderlich.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if transaction_is_closed(db, transaction_id):
            flash("Dieses Geschäftsjahr ist abgeschlossen und gegen Änderungen gesperrt.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        category_id = request.form.get("category_id") or None
        status = request.form.get("receipt_status", "missing")
        if status not in {"missing", "complete", "not_required"}:
            abort(400)
        if category_id and db.execute(
            "SELECT 1 FROM transaction_splits WHERE transaction_id=?", (transaction_id,)
        ).fetchone():
            flash("Entferne zuerst die Splitbuchung, bevor du eine Gesamtkategorie zuordnest.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
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
        if transaction_is_adjustment(db, transaction_id):
            flash("Storno- und Korrekturbuchungen sind unveränderlich.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if transaction_is_closed(db, transaction_id):
            flash("Dieses Geschäftsjahr ist abgeschlossen und gegen Änderungen gesperrt.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
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

    @app.post("/transactions/<int:transaction_id>/splits")
    @login_required
    def transaction_splits_update(transaction_id):
        db = get_db()
        transaction = db.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        if transaction is None:
            abort(404)
        if transaction_is_adjustment(db, transaction_id):
            flash("Storno- und Korrekturbuchungen sind unveränderlich.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if year_is_closed(db, transaction["booking_date"][:4]):
            flash("Dieses Geschäftsjahr ist abgeschlossen und gegen Änderungen gesperrt.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        category_ids = request.form.getlist("split_category_id")
        amounts = request.form.getlist("split_amount")
        notes = request.form.getlist("split_note")
        parsed = []
        sign = 1 if transaction["amount_cents"] >= 0 else -1
        for category_id, amount, note in zip(category_ids, amounts, notes):
            if not category_id and not amount.strip():
                continue
            try:
                cents = abs(parse_amount_cents(amount)) * sign
                category_id_int = int(category_id)
            except (ValueError, TypeError):
                flash("Bitte jede Aufteilungszeile vollständig ausfüllen.", "error")
                return redirect(url_for("transaction_detail", transaction_id=transaction_id))
            if cents == 0:
                flash("Aufteilungsbeträge müssen größer als null sein.", "error")
                return redirect(url_for("transaction_detail", transaction_id=transaction_id))
            if db.execute("SELECT 1 FROM categories WHERE id=? AND active=1", (category_id_int,)).fetchone() is None:
                abort(400)
            parsed.append((category_id_int, cents, note.strip()))
        if len(parsed) < 2:
            flash("Eine Splitbuchung benötigt mindestens zwei Aufteilungszeilen.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if sum(item[1] for item in parsed) != transaction["amount_cents"]:
            flash("Die Aufteilung muss exakt dem Buchungsbetrag entsprechen.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        db.execute("DELETE FROM transaction_splits WHERE transaction_id=?", (transaction_id,))
        db.executemany(
            """INSERT INTO transaction_splits(transaction_id,category_id,amount_cents,note)
               VALUES (?,?,?,?)""",
            [(transaction_id, *item) for item in parsed],
        )
        db.execute(
            "UPDATE transactions SET category_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (transaction_id,),
        )
        log_action(db, "split", "transaction", transaction_id, json.dumps(parsed))
        db.commit()
        flash("Buchung vollständig aufgeteilt.", "success")
        return redirect(url_for("transaction_detail", transaction_id=transaction_id))

    @app.post("/transactions/<int:transaction_id>/splits/clear")
    @login_required
    def transaction_splits_clear(transaction_id):
        db = get_db()
        if transaction_is_adjustment(db, transaction_id):
            flash("Storno- und Korrekturbuchungen sind unveränderlich.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if transaction_is_closed(db, transaction_id):
            flash("Dieses Geschäftsjahr ist abgeschlossen und gegen Änderungen gesperrt.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        db.execute("DELETE FROM transaction_splits WHERE transaction_id=?", (transaction_id,))
        log_action(db, "unsplit", "transaction", transaction_id)
        db.commit()
        flash("Aufteilung entfernt; bitte wieder eine Kategorie zuordnen.", "success")
        return redirect(url_for("transaction_detail", transaction_id=transaction_id))

    @app.post("/transactions/<int:transaction_id>/adjust")
    @login_required
    def transaction_adjust(transaction_id):
        db = get_db()
        original = db.execute(
            """SELECT t.*,a.kind account_kind,a.currency account_currency
               FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE t.id=? AND a.organization_id=?""",
            (transaction_id, ORGANIZATION_ID),
        ).fetchone()
        if original is None:
            abort(404)
        if original["account_kind"] != "cash" or original["bank_transaction_code"] != "CASH":
            flash("Nur manuelle Barkassenbuchungen können in Vereinskasse storniert werden.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if year_is_closed(db, original["booking_date"][:4]):
            flash("Öffne zuerst das Geschäftsjahr der ursprünglichen Buchung wieder.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if transaction_is_adjustment(db, transaction_id):
            flash("Diese Buchung wurde bereits storniert oder korrigiert.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))

        action = request.form.get("action")
        reason = request.form.get("reason", "").strip()
        if action not in {"reverse", "correct"} or len(reason) < 3:
            flash("Bitte Storno oder Korrektur wählen und einen nachvollziehbaren Grund angeben.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        try:
            adjustment_date = datetime.fromisoformat(request.form.get("booking_date", "")).date().isoformat()
        except (TypeError, ValueError):
            flash("Bitte ein gültiges Buchungsdatum für die Gegenbuchung angeben.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if year_is_closed(db, adjustment_date[:4]):
            flash("Die Gegenbuchung kann nicht in einem abgeschlossenen Geschäftsjahr liegen.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        if adjustment_date[:4] != original["booking_date"][:4]:
            flash("Original, Gegenbuchung und Ersatzbuchung müssen im selben Geschäftsjahr liegen.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))

        original_splits = db.execute(
            "SELECT category_id,amount_cents,note FROM transaction_splits WHERE transaction_id=? ORDER BY id",
            (transaction_id,),
        ).fetchall()
        replacement_amount = None
        replacement_purpose = ""
        replacement_category = None
        if action == "correct":
            try:
                replacement_amount = parse_amount_cents(request.form.get("new_amount"))
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("transaction_detail", transaction_id=transaction_id))
            replacement_purpose = request.form.get("new_purpose", "").strip()
            replacement_category = request.form.get("new_category_id") or original["category_id"]
            if replacement_amount == 0 or not replacement_purpose:
                flash("Für die Korrektur werden ein Betrag ungleich null und ein Verwendungszweck benötigt.", "error")
                return redirect(url_for("transaction_detail", transaction_id=transaction_id))
            copy_splits = bool(original_splits) and replacement_amount == original["amount_cents"] and not request.form.get("new_category_id")
            if original_splits and not copy_splits and not request.form.get("new_category_id"):
                flash("Bei geändertem Betrag einer Splitbuchung muss eine neue Gesamtkategorie gewählt werden.", "error")
                return redirect(url_for("transaction_detail", transaction_id=transaction_id))
            if not original_splits and not replacement_category:
                flash("Bitte der korrigierten Buchung eine Kategorie zuordnen.", "error")
                return redirect(url_for("transaction_detail", transaction_id=transaction_id))
            if replacement_category and db.execute(
                "SELECT 1 FROM categories WHERE id=? AND active=1", (replacement_category,)
            ).fetchone() is None:
                abort(400, "Ungültige Kategorie")
        else:
            copy_splits = False

        marker = uuid.uuid4().hex
        batch = db.execute(
            """INSERT INTO import_batches(account_id,filename,file_hash,stored_path,account_iban,imported_count)
               VALUES (?,?,?,?,?,?)""",
            (
                original["account_id"],
                "Storno/Korrektur",
                f"manual:adjustment:{marker}",
                "",
                original["account_iban"],
                2 if action == "correct" else 1,
            ),
        )

        def insert_adjustment(amount, purpose, code, category_id, receipt_status, fingerprint_suffix):
            cursor = db.execute(
                """INSERT INTO transactions(
                   account_id,import_batch_id,fingerprint,account_iban,booking_date,value_date,
                   amount_cents,currency,counterparty,counterparty_iban,purpose,bank_reference,
                   bank_transaction_code,category_id,receipt_status,note
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    original["account_id"], batch.lastrowid,
                    hashlib.sha256(f"adjustment:{marker}:{fingerprint_suffix}".encode()).hexdigest(),
                    original["account_iban"], adjustment_date, adjustment_date, amount,
                    original["currency"], original["counterparty"], original["counterparty_iban"],
                    purpose, f"Buchung #{transaction_id}", code, category_id, receipt_status, reason,
                ),
            )
            return cursor.lastrowid

        reversal_id = insert_adjustment(
            -original["amount_cents"],
            f"Storno zu Buchung #{transaction_id}: {original['purpose'] or 'ohne Verwendungszweck'}",
            "REVERSAL",
            original["category_id"],
            "not_required",
            "reversal",
        )
        if original_splits:
            db.executemany(
                """INSERT INTO transaction_splits(transaction_id,category_id,amount_cents,note)
                   VALUES (?,?,?,?)""",
                [(reversal_id, row["category_id"], -row["amount_cents"], row["note"]) for row in original_splits],
            )

        replacement_id = None
        if action == "correct":
            replacement_id = insert_adjustment(
                replacement_amount,
                replacement_purpose,
                "CORRECTION",
                None if copy_splits else replacement_category,
                original["receipt_status"],
                "replacement",
            )
            if copy_splits:
                db.executemany(
                    """INSERT INTO transaction_splits(transaction_id,category_id,amount_cents,note)
                       VALUES (?,?,?,?)""",
                    [(replacement_id, row["category_id"], row["amount_cents"], row["note"]) for row in original_splits],
                )

        cursor = db.execute(
            """INSERT INTO transaction_adjustments(
               original_transaction_id,reversal_transaction_id,replacement_transaction_id,kind,reason
               ) VALUES (?,?,?,?,?)""",
            (transaction_id, reversal_id, replacement_id, "correction" if action == "correct" else "reversal", reason),
        )
        log_action(
            db,
            "corrected" if action == "correct" else "reversed",
            "transaction_adjustment",
            cursor.lastrowid,
            json.dumps(
                {
                    "original": transaction_id,
                    "reversal": reversal_id,
                    "replacement": replacement_id,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
        )
        db.commit()
        flash(
            "Korrektur mit Gegen- und Ersatzbuchung erstellt."
            if action == "correct"
            else "Stornobuchung erstellt; die ursprüngliche Buchung bleibt erhalten.",
            "success",
        )
        return redirect(
            url_for("transaction_detail", transaction_id=replacement_id or reversal_id)
        )

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
        if year_is_closed(db, booking_date[:4]):
            flash("Dieses Geschäftsjahr ist abgeschlossen; dort sind keine neuen Barbuchungen möglich.", "error")
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

    def complete_import(report, temporary, file_hash, original_filename, selected_account_id=None):
        db = get_db()
        if db.execute("SELECT 1 FROM import_batches WHERE file_hash=?", (file_hash,)).fetchone():
            temporary.unlink(missing_ok=True)
            flash("Diese Kontoauszugsdatei wurde bereits importiert.", "error")
            return redirect(url_for("import_file"))
        closed_years = sorted(
            {tx.booking_date[:4] for tx in report.transactions if year_is_closed(db, tx.booking_date[:4])}
        )
        if closed_years:
            temporary.unlink(missing_ok=True)
            flash(
                "Die Datei enthält Buchungen in abgeschlossenen Jahren: " + ", ".join(closed_years),
                "error",
            )
            return redirect(url_for("import_file"))

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

        safe_filename = secure_filename(original_filename) or "kontoauszug"
        suffix = Path(safe_filename).suffix.lower()
        final_path = Path(app.config["DATA_DIR"]) / "imports" / f"{file_hash}{suffix}"
        shutil.move(temporary, final_path)
        cursor = db.execute(
            """INSERT INTO import_batches(account_id,filename,file_hash,stored_path,account_iban)
               VALUES (?,?,?,?,?)""",
            (
                account["id"],
                safe_filename,
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
            json.dumps({"filename": original_filename, "imported": imported, "duplicates": duplicates}),
        )
        db.commit()
        flash(f"Import abgeschlossen: {imported} neue Buchungen, {duplicates} Duplikate.", "success")
        return redirect(url_for("transactions", status="uncategorized"))

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
            flash("Bitte eine CAMT- oder MT940-Datei auswählen.", "error")
            return redirect(url_for("import_file"))
        suffix = Path(secure_filename(upload.filename)).suffix.lower()
        parser = parse_camt if suffix == ".xml" else parse_mt940 if suffix in {".mt940", ".sta", ".txt"} else None
        if parser is None:
            flash("Direkt unterstützt werden XML (CAMT) sowie MT940-, STA- und TXT-Dateien.", "error")
            return redirect(url_for("import_file"))
        temporary = Path(app.config["DATA_DIR"]) / "imports" / f"tmp-{uuid.uuid4().hex}{suffix}"
        upload.save(temporary)
        file_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        try:
            report = parser(temporary)
        except ValueError as exc:
            temporary.unlink(missing_ok=True)
            flash(str(exc), "error")
            return redirect(url_for("import_file"))
        return complete_import(
            report,
            temporary,
            file_hash,
            upload.filename,
            request.form.get("account_id"),
        )

    @app.post("/import/csv/preview")
    @login_required
    def import_csv_preview():
        upload = request.files.get("statement")
        account_id = request.form.get("account_id")
        if not upload or not upload.filename or not account_id:
            flash("Bitte CSV-Datei und zugehöriges Bankkonto auswählen.", "error")
            return redirect(url_for("import_file"))
        db = get_db()
        account = db.execute(
            """SELECT * FROM accounts WHERE id=? AND organization_id=?
               AND kind='bank' AND active=1""",
            (account_id, ORGANIZATION_ID),
        ).fetchone()
        if account is None:
            abort(400, "Ungültiges Bankkonto")
        previous = session.get("csv_preview") or {}
        previous_token = previous.get("token", "")
        if re.fullmatch(r"[0-9a-f]{48}", str(previous_token)):
            previous_path = Path(app.config["DATA_DIR"]) / "imports" / f"preview-{previous_token}.csv"
            previous_path.unlink(missing_ok=True)
        token = secrets.token_hex(24)
        temporary = Path(app.config["DATA_DIR"]) / "imports" / f"preview-{token}.csv"
        upload.save(temporary)
        try:
            preview = preview_csv(temporary)
        except ValueError as exc:
            temporary.unlink(missing_ok=True)
            flash(str(exc), "error")
            return redirect(url_for("import_file"))
        session["csv_preview"] = {
            "token": token,
            "filename": secure_filename(upload.filename) or "kontoauszug.csv",
            "account_id": int(account_id),
        }
        return render_template("import_csv_preview.html", preview=preview, account=account, token=token)

    @app.post("/import/csv/complete")
    @login_required
    def import_csv_complete():
        saved = session.get("csv_preview") or {}
        token = request.form.get("token", "")
        if not secrets.compare_digest(token, str(saved.get("token", ""))):
            abort(400, "Ungültige oder abgelaufene CSV-Vorschau")
        temporary = Path(app.config["DATA_DIR"]) / "imports" / f"preview-{token}.csv"
        if not temporary.exists():
            abort(400, "Die CSV-Vorschau ist nicht mehr verfügbar")
        db = get_db()
        account = db.execute(
            """SELECT * FROM accounts WHERE id=? AND organization_id=?
               AND kind='bank' AND active=1""",
            (saved.get("account_id"), ORGANIZATION_ID),
        ).fetchone()
        if account is None:
            temporary.unlink(missing_ok=True)
            abort(400, "Ungültiges Bankkonto")
        fields = (
            "booking_date", "value_date", "amount", "counterparty", "purpose",
            "reference", "currency", "counterparty_iban",
        )
        mapping = {field: request.form.get(field, "") for field in fields}
        try:
            report = parse_csv(temporary, mapping, account["iban"] or "")
        except ValueError as exc:
            temporary.unlink(missing_ok=True)
            session.pop("csv_preview", None)
            flash(str(exc), "error")
            return redirect(url_for("import_file"))
        session.pop("csv_preview", None)
        return complete_import(
            report,
            temporary,
            hashlib.sha256(temporary.read_bytes()).hexdigest(),
            saved["filename"],
            account["id"],
        )

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
                          a.name account_name, a.kind account_kind,
                          (SELECT kind FROM transaction_adjustments x WHERE x.original_transaction_id=t.id) adjustment_kind,
                          CASE
                            WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.reversal_transaction_id=t.id) THEN 'reversal'
                            WHEN EXISTS(SELECT 1 FROM transaction_adjustments x WHERE x.replacement_transaction_id=t.id) THEN 'replacement'
                          END adjustment_role
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
                item["splits"] = [
                    dict(split)
                    for split in db.execute(
                        """SELECT s.amount_cents,s.note,c.name category_name,c.tax_area
                           FROM transaction_splits s JOIN categories c ON c.id=s.category_id
                           WHERE s.transaction_id=? ORDER BY s.id""",
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
            "uncategorized": sum(row["category_id"] is None and not row.get("splits") for row in transactions),
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

    def year_status(db, year):
        totals = db.execute(
            """SELECT COUNT(*) total,
                      COALESCE(SUM(receipt_status='missing'),0) missing,
                      COALESCE(SUM(category_id IS NULL AND NOT EXISTS(
                          SELECT 1 FROM transaction_splits s WHERE s.transaction_id=transactions.id
                      )),0) uncategorized
               FROM transactions WHERE substr(booking_date,1,4)=?""",
            (year,),
        ).fetchone()
        closure = db.execute(
            "SELECT * FROM year_closures WHERE organization_id=? AND year=?",
            (ORGANIZATION_ID, year),
        ).fetchone()
        return {**dict(totals), "year": year, "closure": closure}

    @app.get("/year-close")
    @login_required
    def year_close():
        db = get_db()
        years = [
            row["year"]
            for row in db.execute(
                """SELECT year FROM (
                       SELECT DISTINCT substr(booking_date,1,4) year FROM transactions
                       UNION SELECT year FROM year_closures
                   ) ORDER BY year DESC"""
            ).fetchall()
        ]
        return render_template("year_close.html", years=[year_status(db, year) for year in years])

    @app.post("/year-close/<year>")
    @login_required
    def year_close_update(year):
        if len(year) != 4 or not year.isdigit():
            abort(400)
        db = get_db()
        action = request.form.get("action", "close")
        if action == "reopen":
            db.execute(
                "DELETE FROM year_closures WHERE organization_id=? AND year=?",
                (ORGANIZATION_ID, year),
            )
            log_action(db, "reopened", "year", None, year)
            db.commit()
            flash(f"Geschäftsjahr {year} wieder geöffnet.", "success")
            return redirect(url_for("year_close"))
        status = year_status(db, year)
        if not status["total"]:
            flash("Ein leeres Geschäftsjahr kann nicht abgeschlossen werden.", "error")
        elif status["missing"] or status["uncategorized"]:
            flash(
                f"Abschluss nicht möglich: {status['missing']} fehlende Belege und "
                f"{status['uncategorized']} Buchungen ohne Kategorie.",
                "error",
            )
        else:
            payload = {
                "transactions": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM transactions WHERE substr(booking_date,1,4)=? ORDER BY id",
                        (year,),
                    ).fetchall()
                ],
                "splits": [
                    dict(row)
                    for row in db.execute(
                        """SELECT s.* FROM transaction_splits s JOIN transactions t ON t.id=s.transaction_id
                           WHERE substr(t.booking_date,1,4)=? ORDER BY s.id""",
                        (year,),
                    ).fetchall()
                ],
                "attachments": [
                    dict(row)
                    for row in db.execute(
                        """SELECT a.id,a.transaction_id,a.file_hash,a.size_bytes FROM attachments a
                           JOIN transactions t ON t.id=a.transaction_id
                           WHERE substr(t.booking_date,1,4)=? ORDER BY a.id""",
                        (year,),
                    ).fetchall()
                ],
                "adjustments": [
                    dict(row)
                    for row in db.execute(
                        """SELECT x.* FROM transaction_adjustments x
                           JOIN transactions t ON t.id=x.original_transaction_id
                           WHERE substr(t.booking_date,1,4)=? ORDER BY x.id""",
                        (year,),
                    ).fetchall()
                ],
            }
            summary_hash = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            db.execute(
                """INSERT INTO year_closures(organization_id,year,summary_hash)
                   VALUES (?,?,?) ON CONFLICT(organization_id,year)
                   DO UPDATE SET summary_hash=excluded.summary_hash,closed_at=CURRENT_TIMESTAMP""",
                (ORGANIZATION_ID, year, summary_hash),
            )
            log_action(db, "closed", "year", None, json.dumps({"year": year, "hash": summary_hash}))
            db.commit()
            flash(f"Geschäftsjahr {year} abgeschlossen und gesperrt.", "success")
        return redirect(url_for("year_close"))

    @app.get("/years/<year>/archive.zip")
    @login_required
    def year_archive(year):
        if len(year) != 4 or not year.isdigit():
            abort(400)
        db = get_db()
        closure = db.execute(
            "SELECT * FROM year_closures WHERE organization_id=? AND year=?",
            (ORGANIZATION_ID, year),
        ).fetchone()
        if closure is None:
            flash("Das Jahresarchiv steht nach dem Jahresabschluss bereit.", "error")
            return redirect(url_for("year_close"))
        transactions = db.execute(
            """SELECT t.*,a.name account_name,c.name category_name,c.tax_area
               FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id
               LEFT JOIN categories c ON c.id=t.category_id
               WHERE substr(t.booking_date,1,4)=? ORDER BY t.booking_date,t.id""",
            (year,),
        ).fetchall()
        organization = db.execute(
            "SELECT * FROM organizations WHERE id=?", (ORGANIZATION_ID,)
        ).fetchone()
        output = io.BytesIO()
        manifest = {}

        def add_file(archive, name, content):
            data = content.encode() if isinstance(content, str) else content
            archive.writestr(name, data)
            manifest[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            csv_output = io.StringIO()
            writer = csv.writer(csv_output, delimiter=";")
            writer.writerow(
                ["Buchung", "Konto", "Datum", "Betrag", "Währung", "Gegenpartei", "Zweck", "Kategorie", "Steuerbereich", "Split-Notiz", "Belegstatus", "Buchungstyp", "Referenz"]
            )
            for transaction in transactions:
                splits = db.execute(
                    """SELECT s.*,c.name category_name,c.tax_area FROM transaction_splits s
                       JOIN categories c ON c.id=s.category_id WHERE s.transaction_id=? ORDER BY s.id""",
                    (transaction["id"],),
                ).fetchall()
                allocations = splits or [transaction]
                for allocation in allocations:
                    writer.writerow(
                        [
                            transaction["id"], transaction["account_name"], transaction["booking_date"],
                            f"{allocation['amount_cents']/100:.2f}".replace(".", ","), transaction["currency"],
                            transaction["counterparty"], transaction["purpose"], allocation["category_name"],
                            allocation["tax_area"], allocation["note"] if splits else "", transaction["receipt_status"],
                            transaction["bank_transaction_code"], transaction["bank_reference"],
                        ]
                    )
            add_file(archive, "buchungen.csv", "\ufeff" + csv_output.getvalue())
            adjustments = db.execute(
                """SELECT x.* FROM transaction_adjustments x
                   JOIN transactions t ON t.id=x.original_transaction_id
                   WHERE substr(t.booking_date,1,4)=? ORDER BY x.id""",
                (year,),
            ).fetchall()
            if adjustments:
                adjustment_output = io.StringIO()
                adjustment_writer = csv.writer(adjustment_output, delimiter=";")
                adjustment_writer.writerow(
                    ["Korrektur", "Art", "Original", "Gegenbuchung", "Ersatzbuchung", "Grund", "Erstellt"]
                )
                for adjustment in adjustments:
                    adjustment_writer.writerow(
                        [
                            adjustment["id"], adjustment["kind"], adjustment["original_transaction_id"],
                            adjustment["reversal_transaction_id"], adjustment["replacement_transaction_id"] or "",
                            adjustment["reason"], adjustment["created_at"],
                        ]
                    )
                add_file(archive, "korrekturen.csv", "\ufeff" + adjustment_output.getvalue())
            attachments = db.execute(
                """SELECT a.*,t.id transaction_id FROM attachments a JOIN transactions t ON t.id=a.transaction_id
                   WHERE substr(t.booking_date,1,4)=? ORDER BY a.id""",
                (year,),
            ).fetchall()
            for attachment in attachments:
                path = Path(app.config["DATA_DIR"]) / attachment["stored_path"]
                if path.exists():
                    add_file(
                        archive,
                        f"belege/{attachment['transaction_id']}/{attachment['id']}-{secure_filename(attachment['original_name'])}",
                        path.read_bytes(),
                    )
            batches = db.execute(
                """SELECT DISTINCT b.* FROM import_batches b JOIN transactions t ON t.import_batch_id=b.id
                   WHERE substr(t.booking_date,1,4)=? AND b.stored_path!='' ORDER BY b.id""",
                (year,),
            ).fetchall()
            for batch in batches:
                path = Path(app.config["DATA_DIR"]) / batch["stored_path"]
                if path.exists():
                    add_file(archive, f"importe/{batch['id']}-{secure_filename(batch['filename'])}", path.read_bytes())
            report = {
                "organization": organization["name"],
                "year": year,
                "closed_at": closure["closed_at"],
                "summary_hash": closure["summary_hash"],
                "transactions": len(transactions),
                "attachments": len(attachments),
                "adjustments": len(adjustments),
            }
            add_file(archive, "pruefbericht.json", json.dumps(report, ensure_ascii=False, indent=2))
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        output.seek(0)
        return send_file(
            output,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"vereinskasse-{year}.zip",
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
