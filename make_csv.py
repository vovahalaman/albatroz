"""Create or update data/panoramas.csv from the panorama files.

Usage (from the project root):
    python make_csv.py

- Scans data/train1/ and data/train2/ (add more folders to SETS if needed).
- Pulls the 14-digit timestamp (YYYYMMDDhhmmss) out of each file name, wherever it sits.
- Safe to re-run: rows you already filled in (label, human_guess, notes) are kept;
  only new panoramas are added.
"""
import csv
import os
import re

SETS = ["train1", "train2"]
CSV_PATH = os.path.join("data", "panoramas.csv")
FIELDS = ["file", "set", "label", "timestamp", "datetime", "human_guess", "human_confidence", "notes"]


def parse_timestamp(name):
    # Matches IMG_20260605_092536_00_066.jpg (date_time) and plain 20260605092536.jpg
    m = re.search(r"(\d{8})_?(\d{6})", name)
    if not m:
        return "", ""
    ts = m.group(1) + m.group(2)
    readable = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
    return ts, readable


def guess_label(name):
    low = name.lower()
    if "non" in low and "urban" in low:
        return "non-Urban"
    if "urban" in low:
        return "Urban"
    return ""


def main():
    existing = {}
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                existing[(row["set"], row["file"])] = row

    rows, added = [], 0
    for s in SETS:
        folder = os.path.join("data", s)
        if not os.path.isdir(folder):
            print(f"skipping missing folder {folder}")
            continue
        for f in sorted(os.listdir(folder)):
            if not f.lower().endswith((".jpg", ".jpeg")):
                continue
            key = (s, f)
            if key in existing:
                rows.append(existing[key])
                continue
            ts, readable = parse_timestamp(f)
            rows.append({
                "file": f, "set": s, "label": guess_label(f),
                "timestamp": ts, "datetime": readable,
                "human_guess": "", "human_confidence": "", "notes": "",
            })
            added += 1

    os.makedirs("data", exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    missing_ts = [r["file"] for r in rows if not r["timestamp"]]
    print(f"{len(rows)} panoramas in {CSV_PATH} ({added} new)")
    if missing_ts:
        print("No timestamp found in these file names:", ", ".join(missing_ts))


if __name__ == "__main__":
    main()
