from __future__ import annotations

import json
from typing import Any

import requests

GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base/"

# 必須與 bet.hkjc.com 白名單查詢逐字相同，否則馬會會回 WHITELIST_ERROR。
RACE_MEETINGS_QUERY = """
fragment raceFragment on Race {
  id
  no
  status
  raceName_en
  raceName_ch
  postTime
  country_en
  country_ch
  distance
  wageringFieldSize
  go_en
  go_ch
  ratingType
  raceTrack {
    description_en
    description_ch
  }
  raceCourse {
    description_en
    description_ch
    displayCode
  }
  claCode
  raceClass_en
  raceClass_ch
  judgeSigns {
    value_en
  }
}

fragment racingBlockFragment on RaceMeeting {
  jpEsts: pmPools(
    oddsTypes: [WIN, PLA, TCE, TRI, FF, QTT, DT, TT, SixUP]
    filters: ["jackpot", "estimatedDividend"]
  ) {
    leg {
      number
      races
    }
    oddsType
    jackpot
    estimatedDividend
    mergedPoolId
  }
  poolInvs: pmPools(
    oddsTypes: [WIN, PLA, QIN, QPL, CWA, CWB, CWC, IWN, FCT, TCE, TRI, FF, QTT, DBL, TBL, DT, TT, SixUP]
  ) {
    id
    leg {
      races
    }
  }
  penetrometerReadings(filters: ["first"]) {
    reading
    readingTime
  }
  hammerReadings(filters: ["first"]) {
    reading
    readingTime
  }
  changeHistories(filters: ["top3"]) {
    type
    time
    raceNo
    runnerNo
    horseName_ch
    horseName_en
    jockeyName_ch
    jockeyName_en
    scratchHorseName_ch
    scratchHorseName_en
    handicapWeight
    scrResvIndicator
  }
}

query raceMeetings($date: String, $venueCode: String) {
  timeOffset {
    rc
  }
  activeMeetings: raceMeetings {
    id
    venueCode
    date
    status
    races {
      no
      postTime
      status
      wageringFieldSize
    }
  }
  raceMeetings(date: $date, venueCode: $venueCode) {
    id
    status
    venueCode
    date
    totalNumberOfRace
    currentNumberOfRace
    dateOfWeek
    meetingType
    totalInvestment
    country {
      code
      namech
      nameen
      seq
    }
    races {
      ...raceFragment
      runners {
        id
        no
        standbyNo
        status
        name_ch
        name_en
        horse {
          id
          code
        }
        color
        barrierDrawNumber
        handicapWeight
        currentWeight
        currentRating
        internationalRating
        gearInfo
        racingColorFileName
        allowance
        trainerPreference
        last6run
        saddleClothNo
        trumpCard
        priority
        finalPosition
        deadHeat
        winOdds
        jockey {
          code
          name_en
          name_ch
        }
        trainer {
          code
          name_en
          name_ch
        }
      }
    }
    obSt: pmPools(oddsTypes: [WIN, PLA]) {
      leg {
        races
      }
      oddsType
      comingleStatus
    }
    poolInvs: pmPools(
      oddsTypes: [WIN, PLA, QIN, QPL, CWA, CWB, CWC, IWN, FCT, TCE, TRI, FF, QTT, DBL, TBL, DT, TT, SixUP]
    ) {
      id
      leg {
        number
        races
      }
      status
      sellStatus
      oddsType
      investment
      mergedPoolId
      lastUpdateTime
    }
    ...racingBlockFragment
    pmPools(oddsTypes: []) {
      id
    }
    jkcInstNo: foPools(oddsTypes: [JKC], filters: ["top"]) {
      instNo
    }
    tncInstNo: foPools(oddsTypes: [TNC], filters: ["top"]) {
      instNo
    }
  }
}"""


# 白名單內的 racing 賠率查詢（與 bet.hkjc.com / hkjc-api horseOddsQuery 逐字相同）
HORSE_ODDS_QUERY = (
    "\n"
    "query racing($date: String, $venueCode: String, $oddsTypes: [OddsType], $raceNo: Int) {\n"
    "  raceMeetings(date: $date, venueCode: $venueCode) {\n"
    "    pmPools(oddsTypes: $oddsTypes, raceNo: $raceNo) {\n"
    "      id\n"
    "      status\n"
    "      sellStatus\n"
    "      oddsType\n"
    "      lastUpdateTime\n"
    "      guarantee\n"
    "      minTicketCost\n"
    "      name_en\n"
    "      name_ch\n"
    "      leg {\n"
    "        number\n"
    "        races\n"
    "      }\n"
    "      cWinSelections {\n"
    "        composite\n"
    "        name_ch\n"
    "        name_en\n"
    "        starters\n"
    "      }\n"
    "      oddsNodes {\n"
    "        combString\n"
    "        oddsValue\n"
    "        hotFavourite\n"
    "        oddsDropValue\n"
    "        bankerOdds {\n"
    "          combString\n"
    "          oddsValue\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}\n"
)


def meeting_date_iso(value: str) -> str:
    """把 YYYY/MM/DD 或 DD/MM/YYYY 轉成 GraphQL 要用的 YYYY-MM-DD。"""
    text = (value or "").strip().replace(".", "/").replace("-", "/")
    parts = [part for part in text.split("/") if part]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return (value or "").strip()
    if len(parts[0]) == 4:
        year, month, day = parts
    elif len(parts[2]) == 4:
        day, month, year = parts
    else:
        return (value or "").strip()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _graphql_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "https://bet.hkjc.com",
        "Referer": "https://bet.hkjc.com/",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
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
        endpoint, headers=_graphql_headers(), data=body.encode("utf-8"), timeout=30
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    if "data" not in data:
        raise RuntimeError(f"GraphQL response missing 'data': {data}")
    return data["data"]


def get_graphql_data(
    date: str,
    venue_code: str,
    *,
    graphql_url: str | None = None,
) -> dict[str, Any]:
    """用白名單內的 raceMeetings 查詢抓取一個賽馬日。"""
    return _post_graphql(
        operation_name="raceMeetings",
        query=RACE_MEETINGS_QUERY,
        variables={
            "date": meeting_date_iso(date),
            "venueCode": venue_code,
        },
        graphql_url=graphql_url,
    )


def get_race_odds_pools(
    date: str,
    venue_code: str,
    race_no: int,
    *,
    odds_types: list[str] | None = None,
    graphql_url: str | None = None,
) -> list[dict[str, Any]]:
    """抓取指定場次的 WIN／PLA 等賠率池（oddsNodes）。"""
    data = _post_graphql(
        operation_name="racing",
        query=HORSE_ODDS_QUERY,
        variables={
            "date": meeting_date_iso(date),
            "venueCode": venue_code,
            "oddsTypes": odds_types or ["WIN", "PLA"],
            "raceNo": int(race_no),
        },
        graphql_url=graphql_url,
    )
    meetings = data.get("raceMeetings") or []
    if not meetings:
        return []
    return list(meetings[0].get("pmPools") or [])
