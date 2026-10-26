"""HKJC 足球：下載賠率與賽果（GraphQL），寫入 football_matches／football_odds。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .db import get_connection, init_db
from .download_sources import KIND_EPL, first_url_for_kind
from .schema import football_schema_sql

GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base/"
KIND_FOOTBALL_ODDS = "football_odds"
KIND_FOOTBALL_RESULTS = "football_results"
EPL_CODE = "EPL"
EPL_NAME = "英格蘭超級聯賽"
EPL_COMPETITION_ID = "50071337"
DEFAULT_EPL_PAGE_URL = (
    f"https://footylogic.com/zh/tournament/league/{EPL_COMPETITION_ID}/fixturesresults"
)
FOOTYLOGIC_API_BASE = "https://api.footylogic.com"
# Footylogic 語言：18＝中文；channelId＝1 桌面站
_FOOTYLOGIC_LANGUAGE_ID = 18
_FOOTYLOGIC_CHANNEL_ID = 1
_HKT = timezone(timedelta(hours=8))

# 必須與 bet.hkjc.com 白名單查詢逐字一致（hkjc-api footballMatchesQuery）。
_FOOTBALL_MATCHES_QUERY = """
query matchList($startIndex: Int, $endIndex: Int,$startDate: String, $endDate: String, $matchIds: [String], $tournIds: [String], $fbOddsTypes: [FBOddsType]!, $fbOddsTypesM: [FBOddsType]!, $inplayOnly: Boolean, $featuredMatchesOnly: Boolean, $frontEndIds: [String], $earlySettlementOnly: Boolean, $showAllMatch: Boolean) {
    matches(startIndex: $startIndex,endIndex: $endIndex, startDate: $startDate, endDate: $endDate, matchIds: $matchIds, tournIds: $tournIds, fbOddsTypes: $fbOddsTypesM, inplayOnly: $inplayOnly, featuredMatchesOnly: $featuredMatchesOnly, frontEndIds: $frontEndIds, earlySettlementOnly: $earlySettlementOnly, showAllMatch: $showAllMatch) {
      id
      frontEndId
      matchDate
      kickOffTime
      status
      updateAt
      sequence
      esIndicatorEnabled
      homeTeam {
        id
        name_en
        name_ch
      }
      awayTeam {
        id
        name_en
        name_ch
      }
      tournament {
        id
        frontEndId
        nameProfileId
        isInteractiveServiceAvailable
        code
        name_en
        name_ch
      }
      isInteractiveServiceAvailable
      inplayDelay
      venue {
        code
        name_en
        name_ch
      }
      tvChannels {
        code
        name_en
        name_ch
      }
      liveEvents {
        id
        code
      }
      featureStartTime
      featureMatchSequence
      poolInfo {
        normalPools
        inplayPools
        sellingPools
        ntsInfo
        entInfo
        definedPools
      }
      runningResult {
        homeScore
        awayScore
        corner
        homeCorner
        awayCorner
      }
      runningResultExtra {
        homeScore
        awayScore
        corner
        homeCorner
        awayCorner
      }
      adminOperation {
        remark {
          typ
        }
      }
      foPools(fbOddsTypes: $fbOddsTypes) {
        id
        status
        oddsType
        instNo
        inplay
        name_ch
        name_en
        updateAt
        expectedSuspendDateTime
        lines {
          lineId
          status
          condition
          main
          combinations {
            combId
            str
            status
            offerEarlySettlement
            currentOdds
            selections {
              selId
              str
              name_ch
              name_en
            }
          }
        }
      }
    }
  }
"""

_HISTORIC_MATCHES_QUERY = """
query matchResults($startDate: String, $endDate: String, $startIndex: Int,$endIndex: Int,$teamId: String) {
    timeOffset {
    fb
    }
    matchNumByDate(startDate: $startDate, endDate: $endDate, teamId: $teamId) {
    total
    }
    matches: matchResult(startDate: $startDate, endDate: $endDate, startIndex: $startIndex,endIndex: $endIndex, teamId: $teamId) {
    id
    status
    frontEndId
    matchDayOfWeek
    matchNumber
    matchDate
    kickOffTime
    sequence
    homeTeam {
        id
        name_en
        name_ch
    }
    awayTeam {
        id
        name_en
        name_ch
    }
    tournament {
        code
        name_en
        name_ch
    }
    results {
        homeResult
        awayResult
        ttlCornerResult
        resultConfirmType
        payoutConfirmed
        stageId
        resultType
        sequence
    }
    poolInfo {
        payoutRefundPools
        refundPools
        ntsInfo
        entInfo
        definedPools
        ngsInfo {
        str
        name_en
        name_ch
        instNo
        }
        agsInfo {
        str
        name_en
        name_ch
        }
    }
    }
}
"""

DEFAULT_ODDS_TYPES = ["HAD", "HDC", "HIL", "CRS"]


@dataclass
class FootballDownloadStats:
    matches: int = 0
    odds_rows: int = 0
    message: str = ""


def _graphql_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "https://bet.hkjc.com",
        "Referer": "https://bet.hkjc.com/ch/football/home",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _post_graphql(
    *,
    operation_name: str,
    query: str,
    variables: dict[str, Any],
    graphql_url: str | None = None,
) -> dict[str, Any]:
    payload = {
        "operationName": operation_name,
        "query": query,
        "variables": variables,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    endpoint = (graphql_url or GRAPHQL_URL).strip() or GRAPHQL_URL
    response = requests.post(
        endpoint, headers=_graphql_headers(), data=body.encode("utf-8"), timeout=45
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    if "data" not in data:
        raise RuntimeError(f"GraphQL response missing 'data': {data}")
    return data["data"]


def _ensure_football_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(football_schema_sql())
    cols = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(football_matches)").fetchall()
    }
    for name, decl in (
        ("home_ht_score", "TEXT DEFAULT ''"),
        ("away_ht_score", "TEXT DEFAULT ''"),
        ("neutral_venue", "TEXT DEFAULT ''"),
        ("odds_home", "TEXT DEFAULT ''"),
        ("odds_draw", "TEXT DEFAULT ''"),
        ("odds_away", "TEXT DEFAULT ''"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE football_matches ADD COLUMN {name} {decl}")


def _team_name(team: dict[str, Any] | None) -> str:
    if not team:
        return ""
    return str(team.get("name_ch") or team.get("name_en") or "").strip()


def _score_from_running(match: dict[str, Any]) -> tuple[str, str, str]:
    rr = match.get("runningResult") or {}
    home = rr.get("homeScore")
    away = rr.get("awayScore")
    corner = rr.get("corner")
    return (
        "" if home is None else str(home),
        "" if away is None else str(away),
        "" if corner is None else str(corner),
    )


def _score_from_results(match: dict[str, Any]) -> tuple[str, str, str]:
    """取全場比分：優先 resultType==1 且 stageId==5，否則取最後一筆 type1。"""
    rows = match.get("results") or []
    type1 = [r for r in rows if int(r.get("resultType") or 0) == 1]
    row: dict[str, Any] | None = None
    if type1:
        ft = [r for r in type1 if int(r.get("stageId") or 0) == 5]
        row = ft[-1] if ft else type1[-1]
    elif rows:
        row = rows[-1]
    if not row:
        return "", "", ""
    corner_raw = row.get("ttlCornerResult")
    corner = ""
    if corner_raw is not None and str(corner_raw) not in {"", "-1"}:
        corner = str(corner_raw)
    return (
        str(row.get("homeResult") if row.get("homeResult") is not None else ""),
        str(row.get("awayResult") if row.get("awayResult") is not None else ""),
        corner,
    )


def _ht_score_from_results(match: dict[str, Any]) -> tuple[str, str]:
    """半場比分：resultType==1 且 stageId==3（上半場結束）。"""
    rows = match.get("results") or []
    type1 = [r for r in rows if int(r.get("resultType") or 0) == 1]
    ht = [r for r in type1 if int(r.get("stageId") or 0) == 3]
    row = ht[-1] if ht else None
    if not row:
        return "", ""
    return (
        str(row.get("homeResult") if row.get("homeResult") is not None else ""),
        str(row.get("awayResult") if row.get("awayResult") is not None else ""),
    )


def _venue_text(match: dict[str, Any]) -> str:
    venue = match.get("venue") or {}
    if isinstance(venue, dict):
        return str(
            venue.get("name_ch") or venue.get("name_en") or venue.get("code") or ""
        ).strip()
    return str(venue or "").strip()


def _neutral_from_match(match: dict[str, Any], venue: str) -> str:
    for key in ("neutralGround", "isNeutral", "neutral"):
        raw = match.get(key)
        if raw is True or str(raw).lower() in {"1", "true", "y", "yes"}:
            return "是"
        if raw is False or str(raw).lower() in {"0", "false", "n", "no"}:
            return "否"
    if "中立" in venue or "neutral" in venue.lower():
        return "是"
    return ""


def _had_odds_from_match(match: dict[str, Any]) -> tuple[str, str, str]:
    """自 foPools 只取非即場 HAD 主／和／客（不採用即場／預測盤）。"""
    best: dict[str, str] = {"home": "", "draw": "", "away": ""}
    for pool in match.get("foPools") or []:
        if str(pool.get("oddsType") or "").strip().upper() != "HAD":
            continue
        if pool.get("inplay"):
            continue
        for line in pool.get("lines") or []:
            for comb in line.get("combinations") or []:
                sels = comb.get("selections") or []
                if sels:
                    sel_name = "／".join(
                        str(s.get("name_ch") or s.get("name_en") or s.get("str") or "")
                        for s in sels
                    )
                else:
                    sel_name = str(comb.get("str") or "")
                side = _had_side(sel_name)
                odds = str(comb.get("currentOdds") or "").strip()
                if side and odds and not best[side]:
                    best[side] = odds
    return best["home"], best["draw"], best["away"]


def _upsert_match(conn: sqlite3.Connection, match: dict[str, Any], *, is_result: bool) -> None:
    mid = str(match.get("id") or "").strip()
    if not mid:
        return
    tourn = match.get("tournament") or {}
    venue = _venue_text(match)
    home_ht = away_ht = ""
    if is_result:
        home_score, away_score, corners = _score_from_results(match)
        home_ht, away_ht = _ht_score_from_results(match)
    else:
        home_score, away_score, corners = _score_from_running(match)
    odds_home, odds_draw, odds_away = _had_odds_from_match(match)
    neutral = _neutral_from_match(match, venue)
    conn.execute(
        """
        INSERT INTO football_matches (
          match_id, front_end_id, match_date, kick_off_time, status,
          tournament_code, tournament_name,
          home_team, away_team, venue,
          home_score, away_score, home_ht_score, away_ht_score,
          neutral_venue, odds_home, odds_draw, odds_away,
          corners, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
          front_end_id=excluded.front_end_id,
          match_date=excluded.match_date,
          kick_off_time=excluded.kick_off_time,
          status=excluded.status,
          tournament_code=excluded.tournament_code,
          tournament_name=excluded.tournament_name,
          home_team=excluded.home_team,
          away_team=excluded.away_team,
          venue=CASE
            WHEN excluded.venue != '' THEN excluded.venue
            ELSE football_matches.venue END,
          home_score=CASE
            WHEN excluded.home_score != '' THEN excluded.home_score
            ELSE football_matches.home_score END,
          away_score=CASE
            WHEN excluded.away_score != '' THEN excluded.away_score
            ELSE football_matches.away_score END,
          home_ht_score=CASE
            WHEN excluded.home_ht_score != '' THEN excluded.home_ht_score
            ELSE football_matches.home_ht_score END,
          away_ht_score=CASE
            WHEN excluded.away_ht_score != '' THEN excluded.away_ht_score
            ELSE football_matches.away_ht_score END,
          neutral_venue=CASE
            WHEN excluded.neutral_venue != '' THEN excluded.neutral_venue
            ELSE football_matches.neutral_venue END,
          odds_home=CASE
            WHEN excluded.odds_home != '' THEN excluded.odds_home
            ELSE football_matches.odds_home END,
          odds_draw=CASE
            WHEN excluded.odds_draw != '' THEN excluded.odds_draw
            ELSE football_matches.odds_draw END,
          odds_away=CASE
            WHEN excluded.odds_away != '' THEN excluded.odds_away
            ELSE football_matches.odds_away END,
          corners=CASE
            WHEN excluded.corners != '' THEN excluded.corners
            ELSE football_matches.corners END,
          updated_at=excluded.updated_at
        """,
        (
            mid,
            str(match.get("frontEndId") or ""),
            str(match.get("matchDate") or "")[:32],
            str(match.get("kickOffTime") or ""),
            str(match.get("status") or ""),
            str(tourn.get("code") or ""),
            str(tourn.get("name_ch") or tourn.get("name_en") or ""),
            _team_name(match.get("homeTeam")),
            _team_name(match.get("awayTeam")),
            venue,
            home_score,
            away_score,
            home_ht,
            away_ht,
            neutral,
            odds_home,
            odds_draw,
            odds_away,
            corners,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def _upsert_odds(conn: sqlite3.Connection, match: dict[str, Any]) -> int:
    mid = str(match.get("id") or "").strip()
    if not mid:
        return 0
    written = 0
    for pool in match.get("foPools") or []:
        odds_type = str(pool.get("oddsType") or "").strip()
        pool_name = str(pool.get("name_ch") or pool.get("name_en") or odds_type)
        inplay = 1 if pool.get("inplay") else 0
        for line in pool.get("lines") or []:
            condition = str(line.get("condition") or "")
            main = 1 if line.get("main") else 0
            for comb in line.get("combinations") or []:
                sels = comb.get("selections") or []
                if sels:
                    sel_name = "／".join(
                        str(s.get("name_ch") or s.get("name_en") or s.get("str") or "")
                        for s in sels
                    )
                else:
                    sel_name = str(comb.get("str") or "")
                conn.execute(
                    """
                    INSERT INTO football_odds (
                      match_id, odds_type, pool_name, inplay, line_condition, main_line,
                      selection, odds, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id, odds_type, line_condition, selection, inplay)
                    DO UPDATE SET
                      pool_name=excluded.pool_name,
                      main_line=excluded.main_line,
                      odds=excluded.odds,
                      status=excluded.status,
                      updated_at=excluded.updated_at
                    """,
                    (
                        mid,
                        odds_type,
                        pool_name,
                        inplay,
                        condition,
                        main,
                        sel_name,
                        str(comb.get("currentOdds") or ""),
                        str(comb.get("status") or ""),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                written += 1
    return written


def download_football_odds(
    db_path: Path,
    *,
    data_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> FootballDownloadStats:
    """下載目前開售／即將開賽的足球賠率（HAD／HDC／HIL／CRS）。"""
    init_db(db_path)
    if progress:
        progress("正在下載足球賠率…")
    if should_cancel and should_cancel():
        return FootballDownloadStats(message="已取消下載（足球賠率）")
    graphql_url = first_url_for_kind(
        data_dir or db_path.parent, KIND_FOOTBALL_ODDS, GRAPHQL_URL
    )
    data = _post_graphql(
        operation_name="matchList",
        query=_FOOTBALL_MATCHES_QUERY,
        variables={
            "fbOddsTypes": DEFAULT_ODDS_TYPES,
            "fbOddsTypesM": DEFAULT_ODDS_TYPES,
            "startDate": None,
            "endDate": None,
            "tournIds": None,
            "matchIds": None,
            "featuredMatchesOnly": False,
            "frontEndIds": None,
            "earlySettlementOnly": False,
            "showAllMatch": False,
            "startIndex": None,
            "endIndex": None,
            "inplayOnly": None,
        },
        graphql_url=graphql_url,
    )
    matches = list(data.get("matches") or [])
    stats = FootballDownloadStats()
    with get_connection(db_path) as conn:
        _ensure_football_schema(conn)
        for match in matches:
            if should_cancel and should_cancel():
                stats.message = "已取消下載（足球賠率）"
                conn.commit()
                return stats
            _upsert_match(conn, match, is_result=False)
            stats.matches += 1
            stats.odds_rows += _upsert_odds(conn, match)
        conn.commit()
    stats.message = f"足球賠率：賽事 {stats.matches}｜賠率列 {stats.odds_rows}"
    return stats


def download_football_results(
    db_path: Path,
    *,
    data_dir: Path | None = None,
    days: int = 7,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> FootballDownloadStats:
    """下載近 N 日足球賽果（分頁；單頁超過約 20 場會被 API 回空）。"""
    init_db(db_path)
    days = max(1, min(31, int(days)))
    end = date.today()
    start = end - timedelta(days=days - 1)
    if progress:
        progress(f"正在下載足球賽果（{start.isoformat()}～{end.isoformat()}）…")
    if should_cancel and should_cancel():
        return FootballDownloadStats(message="已取消下載（足球賽果）")
    graphql_url = first_url_for_kind(
        data_dir or db_path.parent, KIND_FOOTBALL_RESULTS, GRAPHQL_URL
    )
    # HKJC 單頁約 20 場；相鄰頁偶有 1 場重疊，以 match id 去重。
    page_size = 20
    start_index = 0
    total = 0
    seen_ids: set[str] = set()
    stats = FootballDownloadStats()
    with get_connection(db_path) as conn:
        _ensure_football_schema(conn)
        while True:
            if should_cancel and should_cancel():
                stats.message = "已取消下載（足球賽果）"
                conn.commit()
                return stats
            data = _post_graphql(
                operation_name="matchResults",
                query=_HISTORIC_MATCHES_QUERY,
                variables={
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "startIndex": start_index,
                    "endIndex": start_index + page_size,
                    "teamId": None,
                },
                graphql_url=graphql_url,
            )
            if not total:
                total = int(
                    (data.get("matchNumByDate") or {}).get("total") or 0
                )
            matches = list(data.get("matches") or [])
            if not matches:
                break
            new_matches = [
                m
                for m in matches
                if str(m.get("id") or "").strip()
                and str(m.get("id")).strip() not in seen_ids
            ]
            if not new_matches:
                break
            if progress:
                progress(
                    f"正在下載足球賽果… {len(seen_ids) + len(new_matches)}"
                    f"/{total or '?'} 場"
                )
            for match in new_matches:
                if should_cancel and should_cancel():
                    stats.message = "已取消下載（足球賽果）"
                    conn.commit()
                    return stats
                mid = str(match.get("id") or "").strip()
                seen_ids.add(mid)
                _upsert_match(conn, match, is_result=True)
                stats.matches += 1
            start_index += page_size
            if total and len(seen_ids) >= total:
                break
        conn.commit()
    stats.message = (
        f"足球賽果：{start.isoformat()}～{end.isoformat()}"
        f"｜{stats.matches}/{total or stats.matches} 場"
    )
    return stats


def normalize_football_date(raw: str | None) -> str:
    """GraphQL matchDate 可能為 2026-10-26+08:00，顯示／篩選用 YYYY-MM-DD。"""
    text = str(raw or "").strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text


FOOTBALL_SCOPE_HK = "hk"
FOOTBALL_SCOPE_EPL = "epl"


def normalize_football_scope(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if text in {FOOTBALL_SCOPE_EPL, "epl_football", "football_epl", "premier"}:
        return FOOTBALL_SCOPE_EPL
    return FOOTBALL_SCOPE_HK


def _football_scope_sql(alias: str = "") -> tuple[str, tuple[str, ...]]:
    """回傳 (SQL 條件, 參數)。hk＝非英超；epl＝英超。"""
    col = f"{alias}.tournament_code" if alias else "tournament_code"
    return (f"upper(ifnull({col}, '')) = ?", (EPL_CODE,))


def list_football_dates(
    db_path: Path,
    *,
    scope: str | None = FOOTBALL_SCOPE_HK,
) -> list[str]:
    """資料庫內有足球賽事的日期（新→舊）；可依香港／英超篩選。"""
    init_db(db_path)
    scope_key = normalize_football_scope(scope)
    scope_sql, scope_params = _football_scope_sql()
    if scope_key == FOOTBALL_SCOPE_HK:
        scope_sql = scope_sql.replace(" = ?", " != ?")
    with get_connection(db_path) as conn:
        _ensure_football_schema(conn)
        rows = conn.execute(
            f"""
            SELECT DISTINCT substr(match_date, 1, 10) AS d
            FROM football_matches
            WHERE length(ifnull(match_date, '')) >= 10
              AND {scope_sql}
            ORDER BY d DESC
            """,
            scope_params,
        ).fetchall()
    return [str(r["d"]) for r in rows if r["d"]]


def fetch_football_matches(
    db_path: Path,
    match_date: str,
    *,
    with_odds_only: bool = False,
    scope: str | None = FOOTBALL_SCOPE_HK,
) -> list[dict[str, str]]:
    """依賽日取賽事列；with_odds_only 時只回傳有賠率的賽事。"""
    init_db(db_path)
    day = normalize_football_date(match_date)
    if not day:
        return []
    scope_key = normalize_football_scope(scope)
    scope_sql_m, scope_params = _football_scope_sql("m")
    scope_sql, _ = _football_scope_sql()
    if scope_key == FOOTBALL_SCOPE_HK:
        scope_sql_m = scope_sql_m.replace(" = ?", " != ?")
        scope_sql = scope_sql.replace(" = ?", " != ?")
    with get_connection(db_path) as conn:
        _ensure_football_schema(conn)
        if with_odds_only:
            rows = conn.execute(
                f"""
                SELECT m.match_id, m.front_end_id, m.match_date, m.kick_off_time,
                       m.status, m.tournament_code, m.tournament_name,
                       m.home_team, m.away_team, m.venue,
                       m.home_score, m.away_score, m.corners
                FROM football_matches m
                WHERE substr(m.match_date, 1, 10) = ?
                  AND {scope_sql_m}
                  AND EXISTS (
                    SELECT 1 FROM football_odds o WHERE o.match_id = m.match_id
                  )
                ORDER BY m.kick_off_time, m.front_end_id
                """,
                (day, *scope_params),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT match_id, front_end_id, match_date, kick_off_time,
                       status, tournament_code, tournament_name,
                       home_team, away_team, venue,
                       home_score, away_score, corners
                FROM football_matches
                WHERE substr(match_date, 1, 10) = ?
                  AND {scope_sql}
                ORDER BY kick_off_time, front_end_id
                """,
                (day, *scope_params),
            ).fetchall()
    out: list[dict[str, str]] = []
    for row in rows:
        out.append(
            {
                "match_id": str(row["match_id"] or ""),
                "front_end_id": str(row["front_end_id"] or ""),
                "match_date": normalize_football_date(str(row["match_date"] or "")),
                "kick_off_time": str(row["kick_off_time"] or ""),
                "status": str(row["status"] or ""),
                "tournament_code": str(row["tournament_code"] or ""),
                "tournament_name": str(row["tournament_name"] or ""),
                "home_team": str(row["home_team"] or ""),
                "away_team": str(row["away_team"] or ""),
                "venue": str(row["venue"] or ""),
                "home_score": str(row["home_score"] or ""),
                "away_score": str(row["away_score"] or ""),
                "corners": str(row["corners"] or ""),
            }
        )
    return out


def fetch_football_odds_rows(db_path: Path, match_id: str) -> list[dict[str, str]]:
    """單場盤口列。"""
    init_db(db_path)
    mid = str(match_id or "").strip()
    if not mid:
        return []
    with get_connection(db_path) as conn:
        _ensure_football_schema(conn)
        rows = conn.execute(
            """
            SELECT odds_type, pool_name, inplay, line_condition, main_line,
                   selection, odds, status
            FROM football_odds
            WHERE match_id = ?
            ORDER BY odds_type, main_line DESC, line_condition, selection
            """,
            (mid,),
        ).fetchall()
    return [
        {
            "odds_type": str(r["odds_type"] or ""),
            "pool_name": str(r["pool_name"] or ""),
            "inplay": "1" if r["inplay"] else "",
            "line_condition": str(r["line_condition"] or ""),
            "main_line": "1" if r["main_line"] else "",
            "selection": str(r["selection"] or ""),
            "odds": str(r["odds"] or ""),
            "status": str(r["status"] or ""),
        }
        for r in rows
    ]


def _had_side(selection: str) -> str:
    text = str(selection or "").strip()
    if not text:
        return ""
    if "和" in text or text.upper() in {"D", "DRAW"}:
        return "draw"
    if "客" in text or text.upper() in {"A", "AWAY"}:
        return "away"
    if "主" in text or text.upper() in {"H", "HOME"}:
        return "home"
    return ""


def _load_had_odds_by_teams(
    conn: sqlite3.Connection, match_date: str
) -> dict[tuple[str, str], dict[str, str]]:
    """依賽日＋主客隊，彙整非即場 HAD 主／和／客（含馬會場次對應英超）。"""
    day = normalize_football_date(match_date)
    if not day:
        return {}
    rows = conn.execute(
        """
        SELECT m.home_team, m.away_team, o.selection, o.odds
        FROM football_matches m
        JOIN football_odds o ON o.match_id = m.match_id
        WHERE substr(m.match_date, 1, 10) = ?
          AND o.odds_type = 'HAD'
          AND ifnull(o.inplay, 0) = 0
          AND ifnull(o.odds, '') != ''
        ORDER BY m.match_id
        """,
        (day,),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        home = str(row["home_team"] or "").strip()
        away = str(row["away_team"] or "").strip()
        if not home or not away:
            continue
        key = (home, away)
        bucket = out.setdefault(key, {"home": "", "draw": "", "away": ""})
        side = _had_side(str(row["selection"] or ""))
        odds = str(row["odds"] or "").strip()
        if side and odds and not bucket[side]:
            bucket[side] = odds
    cached = conn.execute(
        """
        SELECT home_team, away_team, odds_home, odds_draw, odds_away
        FROM football_matches
        WHERE substr(match_date, 1, 10) = ?
          AND (
            ifnull(odds_home, '') != ''
            OR ifnull(odds_draw, '') != ''
            OR ifnull(odds_away, '') != ''
          )
        """,
        (day,),
    ).fetchall()
    for row in cached:
        home = str(row["home_team"] or "").strip()
        away = str(row["away_team"] or "").strip()
        if not home or not away:
            continue
        key = (home, away)
        bucket = out.setdefault(key, {"home": "", "draw": "", "away": ""})
        if not bucket["home"]:
            bucket["home"] = str(row["odds_home"] or "").strip()
        if not bucket["draw"]:
            bucket["draw"] = str(row["odds_draw"] or "").strip()
        if not bucket["away"]:
            bucket["away"] = str(row["odds_away"] or "").strip()
    return out


def _epl_row_priority(row: sqlite3.Row | dict[str, Any]) -> tuple[int, int, int, str]:
    """去重時優先：有全場比分 > 有半場 > Footylogic id > match_id。"""
    mid = str(row["match_id"] or "")
    has_ft = 1 if (str(row["home_score"] or "") or str(row["away_score"] or "")) else 0
    has_ht = (
        1 if (str(row["home_ht_score"] or "") or str(row["away_ht_score"] or "")) else 0
    )
    is_footy = 1 if "T" in mid and mid.count("-") >= 3 else 0
    return (has_ft, has_ht, is_footy, mid)


def _load_had_odds_by_match_id(
    conn: sqlite3.Connection, match_date: str
) -> dict[str, dict[str, str]]:
    """依 match_id 彙整當日非即場 HAD 主／和／客。"""
    day = normalize_football_date(match_date)
    if not day:
        return {}
    rows = conn.execute(
        """
        SELECT o.match_id, o.selection, o.odds
        FROM football_odds o
        JOIN football_matches m ON m.match_id = o.match_id
        WHERE substr(m.match_date, 1, 10) = ?
          AND o.odds_type = 'HAD'
          AND ifnull(o.inplay, 0) = 0
          AND ifnull(o.odds, '') != ''
        ORDER BY o.match_id
        """,
        (day,),
    ).fetchall()
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        mid = str(row["match_id"] or "").strip()
        if not mid:
            continue
        bucket = out.setdefault(mid, {"home": "", "draw": "", "away": ""})
        side = _had_side(str(row["selection"] or ""))
        odds = str(row["odds"] or "").strip()
        if side and odds and not bucket[side]:
            bucket[side] = odds
    return out


def fetch_football_display_rows(
    db_path: Path,
    match_date: str,
    *,
    scope: str | None = FOOTBALL_SCOPE_HK,
) -> list[dict[str, str]]:
    """顯示列：聯賽／日期／時間／主客／中立／入球／半場／主和客賠率。"""
    init_db(db_path)
    day = normalize_football_date(match_date)
    if not day:
        return []
    scope_key = normalize_football_scope(scope)
    scope_sql, scope_params = _football_scope_sql()
    if scope_key == FOOTBALL_SCOPE_HK:
        scope_sql = scope_sql.replace(" = ?", " != ?")
    with get_connection(db_path) as conn:
        _ensure_football_schema(conn)
        rows = conn.execute(
            f"""
            SELECT match_id, match_date, kick_off_time, tournament_name, tournament_code,
                   home_team, away_team, venue, neutral_venue,
                   home_score, away_score, home_ht_score, away_ht_score,
                   odds_home, odds_draw, odds_away, status
            FROM football_matches
            WHERE substr(match_date, 1, 10) = ?
              AND {scope_sql}
            ORDER BY kick_off_time, home_team, away_team
            """,
            (day, *scope_params),
        ).fetchall()
        had_by_id = _load_had_odds_by_match_id(conn, day)
        had_by_teams = _load_had_odds_by_teams(conn, day)
    # 英超去重（舊 openfootball／Footylogic 可能並存）
    if scope_key == FOOTBALL_SCOPE_EPL:
        best: dict[tuple[str, str], Any] = {}
        for row in rows:
            home = str(row["home_team"] or "").strip()
            away = str(row["away_team"] or "").strip()
            if not home or not away:
                continue
            key = (home, away)
            prev = best.get(key)
            if prev is None or _epl_row_priority(row) > _epl_row_priority(prev):
                best[key] = row
        rows = list(best.values())
    out: list[dict[str, str]] = []
    for row in sorted(
        rows,
        key=lambda r: (
            str(r["kick_off_time"] or ""),
            str(r["home_team"] or ""),
            str(r["away_team"] or ""),
        ),
    ):
        home = str(row["home_team"] or "")
        away = str(row["away_team"] or "")
        mid = str(row["match_id"] or "")
        day_text, time_text = _kickoff_date_time(
            str(row["match_date"] or ""), str(row["kick_off_time"] or "")
        )
        had = had_by_id.get(mid) or had_by_teams.get((home, away), {})
        odds_home = str(row["odds_home"] or "").strip() or had.get("home", "")
        odds_draw = str(row["odds_draw"] or "").strip() or had.get("draw", "")
        odds_away = str(row["odds_away"] or "").strip() or had.get("away", "")
        neutral = _neutral_label(str(row["neutral_venue"] or ""))
        if not neutral:
            venue = str(row["venue"] or "")
            if "中立" in venue or "neutral" in venue.lower():
                neutral = "是"
        league = (
            str(row["tournament_name"] or "")
            or str(row["tournament_code"] or "")
            or (EPL_NAME if scope_key == FOOTBALL_SCOPE_EPL else "")
        )
        out.append(
            {
                "match_id": mid,
                "league": league,
                "match_date": day_text,
                "kick_off_time": time_text,
                "home_team": home,
                "away_team": away,
                "neutral_venue": neutral,
                "ft_score": _format_score_ratio(
                    str(row["home_score"] or ""), str(row["away_score"] or "")
                ),
                "ht_score": _format_score_ratio(
                    str(row["home_ht_score"] or ""), str(row["away_ht_score"] or "")
                ),
                "odds_home": odds_home,
                "odds_draw": odds_draw,
                "odds_away": odds_away,
                "status": str(row["status"] or ""),
            }
        )
    return out


def fetch_football_epl_display_rows(
    db_path: Path,
    match_date: str,
) -> list[dict[str, str]]:
    """英超顯示列（相容舊呼叫）。"""
    return fetch_football_display_rows(
        db_path, match_date, scope=FOOTBALL_SCOPE_EPL
    )


def _footylogic_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Origin": "https://footylogic.com",
        "Referer": "https://footylogic.com/",
    }


def _parse_epl_competition_id(page_url: str) -> str:
    text = str(page_url or "").strip()
    m = re.search(r"/tournament/league/(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"competitionId=(\d+)", text)
    if m:
        return m.group(1)
    return EPL_COMPETITION_ID


def _footylogic_api_base(page_url: str) -> str:
    parsed = urlparse(str(page_url or "").strip())
    host = (parsed.hostname or "").lower()
    if host in {"api.footylogic.com", "footylogic.com", "www.footylogic.com", "m.footylogic.com"}:
        return FOOTYLOGIC_API_BASE
    if host.endswith("footylogic.com") and "api" in host:
        return f"{parsed.scheme or 'https'}://{host}"
    return FOOTYLOGIC_API_BASE


def _footylogic_get_json(
    url: str,
    *,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = requests.get(
        url,
        params=params,
        headers=_footylogic_headers(),
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Footylogic 回傳格式異常：{url}")
    code = payload.get("statusCode")
    if code not in (None, 200, "200"):
        raise RuntimeError(
            f"Footylogic API 錯誤 {code}：{payload.get('message') or url}"
        )
    return payload


def _parse_footylogic_kickoff(raw: Any) -> tuple[str, str]:
    """回傳 (match_date YYYY-MM-DD HKT, kick_off_time ISO)。"""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_HKT)
        return local.strftime("%Y-%m-%d"), local.isoformat(timespec="seconds")
    except ValueError:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10], text
        return "", text


def _score_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "null", "none", "-"}:
        return ""
    return text


def _format_score_ratio(home: str, away: str) -> str:
    if home == "" and away == "":
        return ""
    return f"{home}-{away}"


def _kickoff_date_time(match_date: str, kick_off_time: str) -> tuple[str, str]:
    day = normalize_football_date(match_date)
    text = str(kick_off_time or "").strip()
    time_text = ""
    if "T" in text:
        tail = text.split("T", 1)[1]
        if len(tail) >= 5 and tail[2] == ":":
            time_text = tail[:5]
    elif len(text) >= 5 and text[2] == ":":
        time_text = text[:5]
    if not day and len(text) >= 10 and text[4] == "-" and text[7] == "-":
        day = text[:10]
    return day, time_text


def _neutral_label(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    if text in {"是", "1", "Y", "y", "true", "True"} or "neutral" in low or "中立" in text:
        return "是"
    if text in {"否", "0", "N", "n", "false", "False"}:
        return "否"
    return text


def _upsert_epl_row(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    front_end_id: str,
    match_date: str,
    kick_off_time: str,
    status: str,
    home_team: str,
    away_team: str,
    home_score: str = "",
    away_score: str = "",
    home_ht_score: str = "",
    away_ht_score: str = "",
    neutral_venue: str = "",
    corners: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO football_matches (
          match_id, front_end_id, match_date, kick_off_time, status,
          tournament_code, tournament_name,
          home_team, away_team, venue,
          home_score, away_score, home_ht_score, away_ht_score,
          neutral_venue, corners, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
          front_end_id=excluded.front_end_id,
          match_date=excluded.match_date,
          kick_off_time=excluded.kick_off_time,
          status=excluded.status,
          tournament_code=excluded.tournament_code,
          tournament_name=excluded.tournament_name,
          home_team=excluded.home_team,
          away_team=excluded.away_team,
          home_score=CASE
            WHEN excluded.home_score != '' THEN excluded.home_score
            ELSE football_matches.home_score END,
          away_score=CASE
            WHEN excluded.away_score != '' THEN excluded.away_score
            ELSE football_matches.away_score END,
          home_ht_score=CASE
            WHEN excluded.home_ht_score != '' THEN excluded.home_ht_score
            ELSE football_matches.home_ht_score END,
          away_ht_score=CASE
            WHEN excluded.away_ht_score != '' THEN excluded.away_ht_score
            ELSE football_matches.away_ht_score END,
          neutral_venue=CASE
            WHEN excluded.neutral_venue != '' THEN excluded.neutral_venue
            ELSE football_matches.neutral_venue END,
          corners=CASE
            WHEN excluded.corners != '' THEN excluded.corners
            ELSE football_matches.corners END,
          updated_at=excluded.updated_at
        """,
        (
            match_id,
            front_end_id,
            match_date,
            kick_off_time,
            status,
            EPL_CODE,
            EPL_NAME,
            home_team,
            away_team,
            "",
            home_score,
            away_score,
            home_ht_score,
            away_ht_score,
            neutral_venue,
            corners,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


# 預設下載近 1 季（約 1 年）賽程／賽果
EPL_SEASONS_TO_DOWNLOAD = 1


def _download_epl_from_footylogic(
    conn: sqlite3.Connection,
    *,
    page_url: str,
    seasons_limit: int = EPL_SEASONS_TO_DOWNLOAD,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[int, int, str]:
    """自 Footylogic 下載近 N 季賽程／賽果。回傳 (寫入場數, 已完賽場數, 球季摘要)。"""
    competition_id = _parse_epl_competition_id(page_url)
    api_base = _footylogic_api_base(page_url).rstrip("/")
    limit = max(1, min(20, int(seasons_limit)))
    if progress:
        progress(f"正在讀取英超球季（Footylogic {competition_id}，近 {limit} 季）…")
    seasons_payload = _footylogic_get_json(
        f"{api_base}/tournament/fixtures-seasons",
        params={
            "channelId": _FOOTYLOGIC_CHANNEL_ID,
            "languageId": _FOOTYLOGIC_LANGUAGE_ID,
            "competitionId": competition_id,
        },
    )
    seasons = list(seasons_payload.get("data") or [])
    if not seasons:
        raise RuntimeError("Footylogic 未回傳英超球季")
    # 清除舊版 openfootball 英超列（EPL-YYYY-MM-DD-隊名），避免與 Footylogic 重複
    conn.execute(
        """
        DELETE FROM football_matches
        WHERE upper(ifnull(tournament_code, '')) = ?
          AND match_id GLOB 'EPL-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*'
        """,
        (EPL_CODE,),
    )
    selected = seasons[:limit]
    season_labels = [
        str(s.get("seasonName") or s.get("seasonId") or "").strip()
        for s in selected
    ]
    season_labels = [x for x in season_labels if x]
    season_summary = "／".join(season_labels) if season_labels else "—"

    written = 0
    results = 0
    seen: set[str] = set()
    tz_offset = int(_HKT.utcoffset(datetime.now()).total_seconds() // 60)
    for season_index, season in enumerate(selected, start=1):
        if should_cancel and should_cancel():
            break
        season_id = season.get("seasonId")
        season_name = str(season.get("seasonName") or "").strip() or str(season_id)
        months = [
            str(m).strip()
            for m in (season.get("fixtureRange") or [])
            if str(m).strip()
        ]
        if not months:
            if progress:
                progress(f"球季 {season_name} 沒有賽程月份，略過")
            continue
        for index, month in enumerate(months, start=1):
            if should_cancel and should_cancel():
                break
            if progress:
                progress(
                    f"正在下載英超賽程/賽果 {season_name}｜{month}"
                    f"（季 {season_index}/{len(selected)}，月 {index}/{len(months)}）…"
                )
            month_payload = _footylogic_get_json(
                f"{api_base}/tournament/fixtures-results",
                params={
                    "languageId": _FOOTYLOGIC_LANGUAGE_ID,
                    "channelId": _FOOTYLOGIC_CHANNEL_ID,
                    "competitionId": competition_id,
                    "seasonId": season_id,
                    "date": month,
                    "tzOffSet": tz_offset,
                },
            )
            info = ((month_payload.get("data") or {}).get("info") or [])
            for day_block in info:
                if should_cancel and should_cancel():
                    break
                day_hint = str(day_block.get("kickOff") or "").strip()
                for event in day_block.get("events") or []:
                    home = str(event.get("homeTeamName") or "").strip()
                    away = str(event.get("awayTeamName") or "").strip()
                    if not home or not away:
                        continue
                    match_date, kick = _parse_footylogic_kickoff(event.get("maUTCKO"))
                    if not match_date and day_hint:
                        match_date = day_hint[:10]
                    if not match_date:
                        continue
                    home_id = str(event.get("homeTeamId") or "").strip()
                    away_id = str(event.get("awayTeamId") or "").strip()
                    mid = (
                        f"EPL-{home_id or home}-{away_id or away}-"
                        f"{str(event.get('maUTCKO') or match_date)}"
                    )
                    if mid in seen:
                        continue
                    seen.add(mid)
                    home_score = _score_text(event.get("homeTeamGoals"))
                    away_score = _score_text(event.get("awayTeamGoals"))
                    home_ht = _score_text(event.get("homeTeamHTGoals"))
                    away_ht = _score_text(event.get("awayTeamHTGoals"))
                    status = (
                        "RESULT"
                        if home_score != "" or away_score != ""
                        else "SCHEDULED"
                    )
                    if status == "RESULT":
                        results += 1
                    _upsert_epl_row(
                        conn,
                        match_id=mid,
                        front_end_id=f"EPL-{match_date}"[:32],
                        match_date=match_date,
                        kick_off_time=kick or match_date,
                        status=status,
                        home_team=home,
                        away_team=away,
                        home_score=home_score,
                        away_score=away_score,
                        home_ht_score=home_ht,
                        away_ht_score=away_ht,
                        neutral_venue="",
                    )
                    written += 1
    return written, results, season_summary


def download_epl_schedule_results(
    db_path: Path,
    *,
    data_dir: Path | None = None,
    result_days: int = 45,
    seasons: int = EPL_SEASONS_TO_DOWNLOAD,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> FootballDownloadStats:
    """下載英格蘭超級聯賽近 N 季賽程／賽果（Footylogic，預設 1 年）。"""
    del result_days  # 改由 Footylogic 依球季月份完整下載
    init_db(db_path)
    page_url = first_url_for_kind(
        data_dir or db_path.parent, KIND_EPL, DEFAULT_EPL_PAGE_URL
    )
    stats = FootballDownloadStats()
    season_summary = ""
    with get_connection(db_path) as conn:
        _ensure_football_schema(conn)
        try:
            written, results, season_summary = _download_epl_from_footylogic(
                conn,
                page_url=page_url,
                seasons_limit=seasons,
                progress=progress,
                should_cancel=should_cancel,
            )
            stats.matches = written
            stats.odds_rows = results
        except Exception as exc:  # noqa: BLE001
            if progress:
                progress(f"英超賽程/賽果來源失敗：{exc}")
            stats.message = f"英超賽程/賽果下載失敗：{exc}"
            conn.commit()
            return stats
        conn.commit()
    if should_cancel and should_cancel():
        stats.message = (
            f"已取消下載（英超賽程/賽果）｜已寫入 {stats.matches} 場"
        )
        return stats
    stats.message = (
        f"英超賽程/賽果：近 {max(1, int(seasons))} 季（{season_summary or '—'}）"
        f"｜寫入 {stats.matches} 場（已完賽 {stats.odds_rows}）"
    )
    return stats
