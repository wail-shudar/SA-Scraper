from __future__ import annotations

import io
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import cast

import pandas as pd
import requests
from dotenv import load_dotenv


CSV_URL = "https://seekingalpha.com/account/undercovered_stocks.csv"
DATA_DIR = Path("data")
SNAPSHOT_PATH = DATA_DIR / "undercovered_stocks_top10.csv"
IT_SECTOR = "Information Technology"
PV_COLUMN = "Quote Page PVs for last 90 days"
PENDING_REVIEW_COLUMN = "Pending Review"

EMAIL_SUBJECT_PREFIX = "SA Ticker Alert"

load_dotenv()


def _load_cookie() -> str:
    """Load the Seeking Alpha session cookie.

    Returns:
        The Seeking Alpha session cookie string.

    Raises:
        ValueError: If ``SA_COOKIE`` is missing from the environment.
    """
    cookie = os.getenv("SA_COOKIE")
    if not cookie:
        raise ValueError("Missing SA_COOKIE. Set it as an environment variable or secret.")
    return cookie


def _read_csv_from_response(response: requests.Response) -> pd.DataFrame:
    """Parse CSV content from an HTTP response.

    Args:
        response: The HTTP response that contains CSV bytes.

    Returns:
        A DataFrame with empty values normalized to empty strings.
    """
    return pd.read_csv(io.BytesIO(response.content), dtype=str).fillna("")


def _filter_it_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """Filter to Information Technology rows and sort by PV.

    Args:
        rows: The source DataFrame containing all rows.

    Returns:
        A filtered DataFrame containing only Information Technology rows.
    """
    filtered = rows[rows["GICS Sector"].eq(IT_SECTOR)].copy()
    if PENDING_REVIEW_COLUMN in filtered.columns:
        pending_review = filtered[PENDING_REVIEW_COLUMN].astype(str).str.upper().eq("TRUE")
        filtered = filtered[~pending_review].copy()
    if PV_COLUMN in filtered.columns:
        filtered[PV_COLUMN] = pd.to_numeric(filtered[PV_COLUMN], errors="coerce").fillna(0).astype(int)
        filtered = filtered.sort_values(by=PV_COLUMN, ascending=False)

    return filtered


def _download_latest_top10(cookie: str) -> pd.DataFrame:
    """Download and trim the current Information Technology watchlist.

    Args:
        cookie: The Seeking Alpha session cookie used for authentication.

    Returns:
        The top 10 Information Technology rows sorted by PV.

    Raises:
        requests.HTTPError: If the CSV request fails.
    """
    headers = {
        "cookie": cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
    }

    response = requests.get(CSV_URL, headers=headers, timeout=60)
    response.raise_for_status()

    latest = _read_csv_from_response(response)
    filtered = _filter_it_rows(latest)
    return filtered.head(10).reset_index(drop=True)


def _load_snapshot() -> pd.DataFrame | None:
    """Load the saved snapshot if it exists.

    Returns:
        The saved snapshot DataFrame, or ``None`` if no snapshot exists.
    """
    if not SNAPSHOT_PATH.exists():
        return None

    snapshot = pd.read_csv(SNAPSHOT_PATH, dtype=str).fillna("")
    if PV_COLUMN in snapshot.columns:
        snapshot[PV_COLUMN] = pd.to_numeric(snapshot[PV_COLUMN], errors="coerce").fillna(0).astype(int)
    return snapshot.head(10).reset_index(drop=True)


def _save_snapshot(top10: pd.DataFrame) -> None:
    """Persist the current snapshot to disk.

    Args:
        top10: The DataFrame to save as the current snapshot.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    top10.to_csv(SNAPSHOT_PATH, index=False)


def _new_entries(previous: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    """Return rows that are present in the current snapshot but not the previous one.

    Args:
        previous: The prior snapshot.
        current: The latest snapshot.

    Returns:
        A DataFrame containing only newly added rows, compared by ticker.
    """
    if previous.empty:
        return current.copy()

    if "Ticker" not in previous.columns or "Ticker" not in current.columns:
        return current.copy()

    previous_tickers = set(previous["Ticker"].astype(str))
    return current[~current["Ticker"].astype(str).isin(previous_tickers)].copy()


def _parse_recipients(raw_recipients: str) -> list[str]:
    """Parse a recipient list from a delimited string.

    Args:
        raw_recipients: A comma- or semicolon-separated string of addresses.

    Returns:
        A list of trimmed recipient email addresses.
    """
    return [entry.strip() for entry in raw_recipients.replace(";", ",").split(",") if entry.strip()]


def _email_settings() -> dict[str, object]:
    """Read SMTP and email delivery settings from the environment.

    Returns:
        A dictionary containing SMTP connection details and recipients.

    Raises:
        ValueError: If required email settings are missing.
    """
    load_dotenv()
    recipients_raw = os.getenv("EMAIL_TO", "")
    recipients = _parse_recipients(recipients_raw)
    if not recipients:
        raise ValueError("Missing EMAIL_TO. Set at least one recipient email address.")

    host = os.getenv("SMTP_HOST")
    if not host:
        raise ValueError("Missing SMTP_HOST. Set your SMTP server host.")

    port_raw = os.getenv("SMTP_PORT")
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    port = int(port_raw) if port_raw else (465 if use_ssl else 587)
    use_starttls = os.getenv("SMTP_USE_STARTTLS", "true").lower() == "true"

    return {
        "host": host,
        "port": port,
        "use_ssl": use_ssl,
        "use_starttls": use_starttls,
        "username": os.getenv("SMTP_USERNAME"),
        "password": os.getenv("SMTP_PASSWORD"),
        "sender": os.getenv("EMAIL_FROM") or os.getenv("SMTP_USERNAME") or recipients[0],
        "recipients": recipients,
    }


def _format_new_entries(frame: pd.DataFrame) -> pd.DataFrame:
    """Prepare new entries for email presentation.

    Args:
        frame: The DataFrame containing newly added rows.

    Returns:
        The DataFrame with the PV column renamed when present.
    """
    if frame.empty:
        return frame

    display = frame.copy()
    if PV_COLUMN in display.columns:
        display = display.rename(columns={PV_COLUMN: "Symbol PV"})

    columns = [column for column in ["Ticker", "Name", "Symbol PV"] if column in display.columns]
    return display.loc[:, columns]


def _build_email(
    new_entries: pd.DataFrame,
    settings: dict[str, object],
) -> EmailMessage:
    """Build the alert email for newly added Information Technology rows.

    Args:
        new_entries: The newly added rows.
        settings: Email delivery settings.

    Returns:
        A multipart email message ready to send.
    """
    subject_prefix = os.getenv("EMAIL_SUBJECT_PREFIX", EMAIL_SUBJECT_PREFIX)
    subject = f"[{subject_prefix}] New Information Technology entries"
    sender = cast(str, settings["sender"])
    recipients = cast(list[str], settings["recipients"])

    new_entries_display = _format_new_entries(new_entries)

    if new_entries_display.empty:
        text_body = "No new Information Technology entries were found."
        html_body = "<html><body><p>No new Information Technology entries were found.</p></body></html>"
    else:
        text_body = new_entries_display.to_string(index=False)
        html_body = f"""
        <html>
          <body style=\"font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;\">
            {new_entries_display.to_html(index=False, border=0)}
          </body>
        </html>
        """

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def _send_email(message: EmailMessage, settings: dict[str, object]) -> None:
    """Send an email message using the configured SMTP transport.

    Args:
        message: The email message to send.
        settings: Email delivery settings.
    """
    smtp_class = smtplib.SMTP_SSL if settings["use_ssl"] else smtplib.SMTP
    host = cast(str, settings["host"])
    port = cast(int, settings["port"])
    use_ssl = cast(bool, settings["use_ssl"])
    use_starttls = cast(bool, settings["use_starttls"])
    username = cast(str | None, settings["username"])
    password = cast(str | None, settings["password"])

    with smtp_class(host, port) as smtp:
        if not use_ssl and use_starttls:
            smtp.starttls()

        if username and password:
            smtp.login(username, password)

        smtp.send_message(message)


def main() -> int:
    """Run the alert workflow.

    Returns:
        Zero on success.
    """
    cookie = _load_cookie()
    previous_top10 = _load_snapshot()
    current_top10 = _download_latest_top10(cookie)

    if previous_top10 is None:
        _save_snapshot(current_top10)
        print(f"Created top 10 Information Technology snapshot at {SNAPSHOT_PATH}.")
        return 0

    new_entries = _new_entries(previous_top10, current_top10)

    if new_entries.empty:
        _save_snapshot(current_top10)
        print("No new Information Technology entries detected.")
        return 0

    email_settings = _email_settings()
    email_message = _build_email(new_entries, email_settings)
    _send_email(email_message, email_settings)
    _save_snapshot(current_top10)

    print(f"Snapshot updated at {SNAPSHOT_PATH} and email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())