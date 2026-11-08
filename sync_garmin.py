"""Fetch a private dashboard snapshot using Garmin's official Personal API client."""
import argparse
import io
import json
import math
import os
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DAY = timedelta(days=1)
BASE_URLS = {"cn": "https://personal.connectapi.garmin.cn", "global": "https://personal.connectapi.garmin.com"}
PAGE_SIZE = 50
MAX_FIT_BYTES = 32 * 1024 * 1024


class SyncError(Exception):
    """A short diagnostic that never includes a remote response or secret."""


def number(value, divisor=1):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return round(value / divisor, 3) if divisor != 1 else value


def records(payload, key=None):
    # The official client returns None for a successful empty HTTP response.
    if payload is None:
        return []
    if key is not None:
        if not isinstance(payload, dict) or key not in payload:
            raise SyncError("API response is missing its expected list.")
        payload = payload[key]
        if payload is None:
            return []
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise SyncError("Unexpected list returned by Garmin.")
    return payload


def dated(rows, start, end):
    result = {}
    for row in rows:
        label = row.get("calendarDate")
        if not isinstance(label, str) or not start.isoformat() <= label <= end.isoformat():
            raise SyncError("Garmin returned a missing or out-of-range measurement date.")
        if label in result:
            raise SyncError("Duplicate measurement date in a Garmin daily response.")
        result[label] = row
    return result


def fetch_days(client, start, end, days):
    summaries = dated(records(client.get_daily_summaries(start, end), "data"), start, end)
    sleep = dated(records(client.get_daily_sleep_summaries(start, end)), start, end)
    hrv = dated(records(client.get_daily_hrv_summaries(start, end), "hrvSummaries"), start, end)
    for offset in range((end - start).days + 1):
        day = (start + offset * DAY).isoformat()
        summary = summaries.get(day, {})
        steps = (summary.get("movement") or {}).get("steps") or {}
        row = days.setdefault(day, {"date": day, "water_l": None, "weight_kg": None})
        incoming = dict(
            sleep_minutes=number(sleep.get(day, {}).get("sleepTimeSeconds"), 60),
            hrv_ms=number(hrv.get(day, {}).get("lastNightAvg")),
            resting_hr_bpm=number((summary.get("heartRate") or {}).get("restingValue")),
            body_battery=number((summary.get("bodyBattery") or {}).get("latestValue")),
            steps=number(steps.get("value")),
        )
        for metric, value in incoming.items():
            if value is not None or metric not in row:
                row[metric] = value


def fetch_activities(client, start, end, activities):
    offset = 0
    seen = set()
    while True:
        page = records(client.search_activities(start=offset, limit=PAGE_SIZE, start_date=start, end_date=end))
        if not page:
            break
        fresh = 0
        for raw in page:
            key = str(raw.get("activityId", ""))
            stamp = raw.get("startTimeLocal")
            if not key.isascii() or not key.isdigit() or not isinstance(stamp, str):
                raise SyncError("Activity is missing a numeric ID or local start time.")
            local = datetime.fromisoformat(stamp)
            if not start <= local.date() <= end:
                raise SyncError("Activity date falls outside the requested range.")
            fresh += key not in seen
            seen.add(key)
            row = activities.setdefault(key, {"id": key})
            row.update(
                date=local.date().isoformat(), start_local=local.isoformat(),
                type=(raw.get("activityType") or {}).get("typeKey") or "unknown",
                distance_km=number(raw.get("distance"), 1000),
                duration_minutes=number(raw.get("duration"), 60),
            )
        if fresh == 0:
            raise SyncError("Activity pagination repeated a page. The previous snapshot was kept.")
        offset += len(page)


def gps_line(original):
    """Return actual FIT coordinates separated by None whenever the recording breaks."""
    from garmin_fit_sdk import Decoder, Stream

    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        fits = [m for m in archive.infolist() if not m.is_dir() and m.filename.lower().endswith(".fit")]
        if len(fits) != 1 or not 0 < fits[0].file_size <= MAX_FIT_BYTES or fits[0].flag_bits & 1:
            return []
        with archive.open(fits[0]) as member:
            content = member.read(MAX_FIT_BYTES + 1)
    if not 0 < len(content) <= MAX_FIT_BYTES:
        return []
    stream = Stream.from_byte_array(content)
    try:
        if not Decoder(stream).check_integrity():
            return []
        stream.seek(0)
        messages, errors = Decoder(stream).read(enable_crc_check=True, merge_heart_rates=False)
    finally:
        stream.close()
    if errors:
        return []
    segments = [[]]
    previous = None
    for sample in messages.get("record_mesgs", []):
        stamp = sample.get("timestamp")
        lat, lon = sample.get("position_lat"), sample.get("position_long")
        if not isinstance(stamp, datetime) or type(lat) is not int or type(lon) is not int:
            previous = None
            continue
        if 0x7fffffff in (lat, lon) or abs(lat) > 2**30 or abs(lon) > 2**31:
            previous = None
            continue
        if segments[-1] and (previous is None or not 0 < (stamp - previous).total_seconds() <= 300):
            segments.append([])
        segments[-1].append([round(lat * 180 / 2**31, 6), round(lon * 180 / 2**31, 6)])
        previous = stamp
    line = []
    for segment in segments:
        if len(segment) >= 2:
            if line:
                line.append(None)
            line.extend(segment)
    return line


def collect(client, old, start, end, with_route):
    days = {row["date"]: row.copy() for row in old["days"]}
    activities = {str(row["id"]): row.copy() for row in old["activities"]}
    cursor = start
    while cursor <= end:
        through = min(end, cursor + 6 * DAY)
        fetch_days(client, cursor, through, days)
        fetch_activities(client, cursor, through, activities)
        cursor = through + DAY
    sorted_activities = sorted(activities.values(), key=lambda r: (r.get("start_local") or r["date"], r["id"]))
    if with_route and sorted_activities:
        last = sorted_activities[-1]
        if not last.get("route"):
            try:
                last["route"] = gps_line(client.download_activity(last["id"]))
                if not last["route"]:
                    print("No continuous GPS track available for the latest activity.", file=sys.stderr)
            except Exception as exc:
                print(f"Latest route unavailable ({type(exc).__name__}). Other records will be saved.", file=sys.stderr)
    return {**old, "days": sorted(days.values(), key=lambda r: r["date"]),
            "activities": sorted_activities, "updated_at": datetime.now(timezone.utc).isoformat()}


def atomic_save(path, snapshot):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".sync-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(snapshot, out, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(args):
    # File locks are released automatically on process exit, including on NAS.
    import fcntl
    from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator, resolve_api_key

    os.umask(0o077)
    folder = args.data_dir
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / "overview.json"
    with (folder / ".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("A sync is already running.")
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"days": [], "activities": []}
        if old.get("demo") is True:
            raise SyncError("This data directory contains demo data. Choose an empty --data-dir for real sync.")
        if old.get("source_region", args.region) != args.region:
            raise SyncError("Use a separate data directory for each Garmin region and account.")
        end = args.end or date.today()
        last_sync_day = old.get("garmin_through")
        start = args.start or (min(date.fromisoformat(last_sync_day), end) - DAY if last_sync_day else end - 6 * DAY)
        if start > end:
            raise SyncError("Start date must not follow end date.")
        if end > date.today():
            raise SyncError("End date must not be in the future.")
        if args.key_file is None and not os.environ.get("GARMIN_PERSONAL_API_KEY"):
            raise SyncError("Supply --key-file or GARMIN_PERSONAL_API_KEY.")
        os.environ["GARMIN_PERSONAL_API_BASE_URL"] = BASE_URLS[args.region]
        secret = resolve_api_key(key_file=args.key_file)
        # Keep the SDK cache outside the repository and discard it on exit.
        with tempfile.TemporaryDirectory() as temp, PersonalApiAuthenticator(
                bearer_token_cache_file=Path(temp) / "bearer.json") as auth:
            token = auth.obtain_token(secret).access_token
            with GarminPersonalApiClient(token) as client:
                snapshot = collect(client, old, start, end, args.latest_route)
        snapshot["source_region"] = args.region
        snapshot["garmin_through"] = max(last_sync_day or end.isoformat(), end.isoformat())
        atomic_save(path, snapshot)
    print(f"Synced {start} through {end}. Saved {len(snapshot['activities'])} known activities.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", choices=BASE_URLS, required=True)
    parser.add_argument("--key-file", type=Path, help="Plain-text API key file containing only your key.")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--start", type=date.fromisoformat, help="First requested local calendar date.")
    parser.add_argument("--end", type=date.fromisoformat, help="Last requested local calendar date (default: today).")
    parser.add_argument("--latest-route", action="store_true", help="Read the latest activity's original ZIP/FIT for genuine GPS points.")
    args = parser.parse_args()
    try:
        run(args)
    except SyncError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ImportError:
        print("Sync requires macOS/Linux and Python 3.12+ with requirements-garmin.txt installed.", file=sys.stderr)
        return 1
    except Exception as exc:
        # SDK errors can include the remote response body. Never echo them.
        status = getattr(exc, "status_code", None)
        extra = f" (HTTP {status})" if type(status) is int else ""
        print(f"Sync failed: {type(exc).__name__}{extra}. The previous snapshot was kept.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
