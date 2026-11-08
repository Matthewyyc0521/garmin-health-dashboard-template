"""Export a private dashboard to Excel and merge only user-entered water and weight."""
import argparse
import json
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zipfile import BadZipFile

from sync_garmin import atomic_save

DATA_DIR = Path(__file__).resolve().parent / "data"
HEALTH = "健康记录"
BASELINE = "_导出基准"
VERSION = "health-workbook-v1"
FIELDS = ["date", "sleep_minutes", "hrv_ms", "resting_hr_bpm", "body_battery", "steps", "water_l", "weight_kg"]
HEADINGS = ["日期", "睡眠（分钟）", "夜间HRV（毫秒）", "静息心率（次/分）", "身体电量", "步数", "手填饮水（升）", "手填体重（公斤）"]
TYPES = {"running": "跑步", "trail_running": "越野跑", "cycling": "骑行", "walking": "步行",
         "hiking": "徒步", "mountaineering": "登山", "swimming": "游泳", "pool_swimming": "泳池游泳",
         "strength_training": "力量训练", "indoor_cycling": "室内骑行"}
MANUAL = ["water_l", "weight_kg"]


def load_snapshot(folder, allow_empty=False):
    path = folder / "overview.json"
    if allow_empty and not path.exists():
        return {"days": [], "activities": []}
    if not path.is_file():
        raise ValueError("没有私人数据，请先同步，或用 template 生成空表。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(data, dict) and isinstance(data.get("days"), list) and isinstance(data.get("activities"), list)):
        raise ValueError("私人概览文件格式不正确。")
    return data


def iso_day(value):
    if isinstance(value, datetime):
        if value.time() != datetime.min.time():
            raise ValueError("日期列只接受自然日。")
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value).isoformat()
    raise ValueError("请使用 Excel 日期或 YYYY-MM-DD 格式。")


def style_sheet(sheet, widths, editable=False):
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    for cell, width in zip(sheet[1], widths):
        cell.fill = PatternFill("solid", fgColor="347353" if editable and cell.column >= 7 else "254637")
        cell.font = Font(color="FFFFFF", bold=True)
        sheet.column_dimensions[get_column_letter(cell.column)].width = width
    sheet.row_dimensions[1].height = 26
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False


def append_values(sheet, values):
    # Explicit text cells preserve third-party strings beginning with "=" as text.
    from openpyxl.cell import Cell

    cells = []
    for value in values:
        cell = Cell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = "s"
        elif isinstance(value, date):
            cell.number_format = "yyyy-mm-dd"
        cells.append(cell)
    sheet.append(cells)


def build_book(snapshot):
    from openpyxl import Workbook

    book = Workbook()
    health = book.active
    health.title = HEALTH
    health.append(HEADINGS)
    base = book.create_sheet(BASELINE)
    base.append([VERSION, "water_l", "weight_kg"])
    base.sheet_state = "hidden"
    for row in sorted(snapshot["days"], key=lambda r: r["date"]):
        day = date.fromisoformat(iso_day(row["date"]))
        append_values(health, [day] + [row.get(key) for key in FIELDS[1:]])
        base.append([day, row.get("water_l"), row.get("weight_kg")])
    style_sheet(health, [16, 18, 20, 21, 14, 14, 23, 23], editable=True)

    groups = defaultdict(list)
    for row in snapshot["activities"]:
        groups[str(row.get("type") or "unknown")].append(row)
    for kind, activities in sorted(groups.items()):
        label = TYPES.get(kind, kind)
        # openpyxl appends a numeric suffix for duplicate short names.
        clean = re.sub(r"[\x00-\x1f\[\]*:/\\?]", "_", label).strip("'")
        sheet = book.create_sheet(("运动-" + (clean or "其他"))[:26])
        sheet.append(["活动ID", "日期", "运动类型", "距离（公里）", "时长（分钟）"])
        for row in sorted(activities, key=lambda r: r["date"]):
            append_values(sheet, [str(row["id"]), date.fromisoformat(iso_day(row["date"])), kind,
                                  row.get("distance_km"), row.get("duration_minutes")])
        style_sheet(sheet, [23, 16, 25, 18, 18])
    return book


def save_book(snapshot, path, overwrite):
    if path.suffix.lower() != ".xlsx":
        raise ValueError("输出文件须以 .xlsx 结尾。")
    if path.exists() and not overwrite:
        raise ValueError("输出已存在。请选择新文件名或明确指定 --overwrite。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".xlsx")
    try:
        with os.fdopen(fd, "wb") as handle:
            book = build_book(snapshot)
            try:
                book.save(handle)
            finally:
                book.close()
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(name, path)
        else:
            os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def manual_number(value, key, row):
    if value is None or value == "":
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"第 {row} 行的手填项须为数值。")
    if value < 0 or (key == "weight_kg" and value == 0):
        raise ValueError(f"第 {row} 行的饮水量须为非负数，体重须大于零。")
    return value


def read_manual(filename):
    from openpyxl import load_workbook

    book = load_workbook(filename, read_only=True, data_only=False, keep_links=False)
    try:
        if HEALTH not in book.sheetnames or BASELINE not in book.sheetnames:
            raise ValueError("请使用此工具 template 或 export 生成的工作簿。")
        health = book[HEALTH]
        if list(next(health.values, ()))[:len(HEADINGS)] != HEADINGS:
            raise ValueError("健康记录表的列标题与当前模板不一致。")
        base = book[BASELINE].values
        if list(next(base, ())) != [VERSION, *MANUAL]:
            raise ValueError("工作簿版本与当前工具不兼容。")
        original = {iso_day(row[0]): row[1:3] for row in base if row[0] is not None}
        edits, seen = {}, set()
        for i, values in enumerate(health.iter_rows(min_row=2, max_col=8, values_only=True), 2):
            if values[0] is None and all(v in (None, "") for v in values[6:8]):
                continue
            day = iso_day(values[0])
            if day in seen:
                raise ValueError(f"日期重复：{day}")
            seen.add(day)
            previous = original.get(day, (None, None))
            for j, key in enumerate(MANUAL):
                new = manual_number(values[j + 6], key, i)
                if new is not None and new != previous[j]:
                    edits[day, key] = (previous[j], new)
        return edits
    finally:
        book.close()


def import_manual(path, folder):
    import fcntl

    edits = read_manual(path)
    if not edits:
        return 0
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / ".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有同步任务正在运行，稍后再导入。") from None
        snapshot = load_snapshot(folder, allow_empty=True)
        rows = {iso_day(row["date"]): row.copy() for row in snapshot["days"]}
        if len(rows) != len(snapshot["days"]):
            raise ValueError("概览中存在重复日期，请先核对源数据。")
        changed = 0
        for (day, key), (original, value) in edits.items():
            row = rows.setdefault(day, dict.fromkeys(FIELDS))
            current = row.get(key)
            if current == value:
                continue
            if current != original:
                raise ValueError(f"{day} 的手填值已在另一次导入中变更。请重新导出后编辑。")
            row["date"] = day
            row[key] = value
            changed += 1
        if changed:
            snapshot["days"] = [rows[day] for day in sorted(rows)]
            snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_save(folder / "overview.json", snapshot)
        return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name, help_text in [("template", "生成一份空白工作簿"), ("export", "导出私人记录"),
                            ("import", "从工作簿按日期合并手填饮水、体重")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("file", type=Path, help="Excel 工作簿路径 (.xlsx)")
        if name != "template":
            command.add_argument("--data-dir", type=Path, default=DATA_DIR)
        if name != "import":
            command.add_argument("--overwrite", action="store_true", help="明确允许替换现有输出文件")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "import":
            print(f"已合并 {import_manual(args.file, args.data_dir)} 个手填值。")
        else:
            snapshot = load_snapshot(args.data_dir) if args.action == "export" else {"days": [], "activities": []}
            save_book(snapshot, args.file, args.overwrite)
            print(f"工作簿已写入 {args.file}")
    except ImportError:
        print("请安装 requirements-excel.txt；导入功能需要 macOS/Linux 或 WSL。", file=sys.stderr)
        return 1
    except (ValueError, OSError, BadZipFile) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
