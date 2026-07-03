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


def _changed_row_numbers(previous: pd.DataFrame, current: pd.DataFrame) -> list[int]:
    """Compute changed row positions between two DataFrames.

    Args:
        previous: The prior snapshot.
        current: The latest snapshot.

    Returns:
        A list of 1-based row positions that changed.
    """
    changed_rows: list[int] = []
    max_rows = max(len(previous), len(current))

    for index in range(max_rows):
        if index >= len(previous) or index >= len(current):
            changed_rows.append(index + 1)
            continue

        if not previous.iloc[index].equals(current.iloc[index]):
            changed_rows.append(index + 1)

    return changed_rows


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


def _rename_pv_column(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename the PV column for email presentation.

    Args:
        frame: The DataFrame to rename.

    Returns:
        The DataFrame with the PV column renamed when present.
    """
    if PV_COLUMN not in frame.columns:
        return frame

    return frame.rename(columns={PV_COLUMN: "Symbol PV"})


def _build_email(
    previous: pd.DataFrame,
    current: pd.DataFrame,
    changed_rows: list[int],
    settings: dict[str, object],
) -> EmailMessage:
    """Build the alert email for changed Information Technology rows.

    Args:
        previous: The previous snapshot DataFrame.
        current: The current snapshot DataFrame.
        changed_rows: The 1-based row numbers that changed.
        settings: Email delivery settings.

    Returns:
        A multipart email message ready to send.
    """
    subject_prefix = os.getenv("EMAIL_SUBJECT_PREFIX", EMAIL_SUBJECT_PREFIX)
    subject = f"[{subject_prefix}] Top 10 Information Technology rows changed"
    sender = cast(str, settings["sender"])
    recipients = cast(list[str], settings["recipients"])

    changed_rows_text = ", ".join(str(row_number) for row_number in changed_rows)
    previous_display = _rename_pv_column(previous.copy())
    current_display = _rename_pv_column(current.copy())

    text_body = (
        "The top 10 Information Technology rows in undercovered_stocks.csv changed.\n\n"
        f"Changed row positions: {changed_rows_text}\n\n"
        "Previous top 10:\n"
        f"{previous_display.to_string(index=False)}\n\n"
        "Current top 10:\n"
        f"{current_display.to_string(index=False)}\n"
    )

    html_body = f"""
    <html>
      <body style=\"font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;\">
                <h2 style=\"margin-bottom: 0.25rem;\">Top 10 Information Technology rows changed</h2>
        <p style=\"margin-top: 0;\">Changed row positions: {changed_rows_text}</p>
        <h3>Previous top 10</h3>
                {previous_display.to_html(index=False, border=0)}
        <h3>Current top 10</h3>
                {current_display.to_html(index=False, border=0)}
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

    if current_top10.equals(previous_top10):
        _save_snapshot(current_top10)
        print("No changes detected in the top 10 Information Technology rows.")
        return 0

    changed_rows = _changed_row_numbers(previous_top10, current_top10)
    email_settings = _email_settings()
    email_message = _build_email(previous_top10, current_top10, changed_rows, email_settings)
    _send_email(email_message, email_settings)
    _save_snapshot(current_top10)

    print(f"Top 10 Information Technology rows changed at positions: {', '.join(str(row) for row in changed_rows)}")
    print(f"Snapshot updated at {SNAPSHOT_PATH} and email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())