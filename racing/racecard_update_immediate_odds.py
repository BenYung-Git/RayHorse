from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from racing.hkjc_graphql import get_graphql_data, meeting_date_iso
except ImportError:  # 直接執行此檔時，racing 目錄在 sys.path 上
    from hkjc_graphql import get_graphql_data, meeting_date_iso


def update_win_odds(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
    horse_no: str,
    win_odds: Any,
) -> None:
    cur = conn.execute(
        """
        UPDATE racecard_starters
           SET "即時賠率" = ?
         WHERE race_date = ?
           AND racecourse = ?
           AND race_no = ?
           AND "馬號" = ?
           AND 後備 = '否';
        """,
        (win_odds, race_date, racecourse, race_no, horse_no),
    )

    if cur.rowcount == 0:
        # 找不到匹配紀錄就略過（只提示，不新增 row）。
        print(
            f"[略過] 未找到匹配：{race_date} {racecourse} 第{race_no}場 馬號={horse_no} 後備=否"
        )


def main() -> None:
    # Assumption per your spec.
    # Allow override via CLI: python ...\racecard_update_immediate_odds.py path\to\racecard.db
    db_path = Path(sys.argv[1]) if len(sys.argv) >= 2 else Path("racecard.db")
    if not db_path.exists():
        raise FileNotFoundError(f"找不到資料庫檔案：{db_path.resolve()}")

    # Fixed variables per your spec.
    date = "2026-03-22"
    venue_code = "ST"

    # 1) Fetch GraphQL JSON
    try:
        data = get_graphql_data(date=date, venue_code=venue_code)
    except Exception as e:
        print(f"GraphQL 抓取失敗：{e}")
        return

    # Expected shape:
    # data["raceMeetings"] -> list of meetings
    meetings = data.get("raceMeetings") or []
    if not meetings:
        print("GraphQL：找不到 raceMeetings。")
        return

    # 2) Update existing records only
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        # In case the script updates many rows, wrap in a transaction.
        for meeting in meetings:
            meeting_date = meeting.get("date") or date
            meeting_venue = meeting.get("venueCode") or venue_code

            races = meeting.get("races") or []
            for race in races:
                race_no = race.get("no")
                if race_no is None:
                    continue

                runners = race.get("runners") or []
                for runner in runners:
                    horse_no = runner.get("no")
                    if horse_no is None:
                        continue

                    win_odds = str(runner.get("winOdds") or "").strip()
                    if not win_odds:
                        continue
                    stored_date = date if meeting_date_iso(str(meeting_date)) == meeting_date_iso(date) else str(meeting_date)
                    stored_venue = venue_code if str(meeting_venue) == venue_code else str(meeting_venue)
                    update_win_odds(
                        conn=conn,
                        race_date=stored_date,
                        racecourse=stored_venue,
                        race_no=int(race_no),
                        horse_no=str(horse_no).strip(),
                        win_odds=win_odds,
                    )

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()

