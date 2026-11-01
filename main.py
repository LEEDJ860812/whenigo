"""언제나갈까? - 백엔드 (FastAPI)
로컬 실행: python -m uvicorn main:app --reload  →  http://localhost:8000
배포(Render): uvicorn main:app --host 0.0.0.0 --port $PORT   (README.md 참고)

환경변수
  NAVER_CLIENT_ID : 네이버 지도 클라이언트 ID (Secret은 지도 표시에 필요 없음)
  GBIS_KEY        : 공공데이터포털 인증키 (경기도·서울시 공통, 없으면 샘플 데이터로 동작)
  SEOUL_KEY       : (선택) 서울시용 인증키를 따로 쓸 때. 없으면 GBIS_KEY 사용
  TMAP_KEY     : TMAP(SK open API) appKey (없으면 직선거리 추정으로 동작)
  SEOUL_SUBWAY_KEY : 서울 열린데이터광장 실시간 지하철 인증키 (없으면 지하철 기능 꺼짐)
"""
import json, math, os, random, re, time
from pathlib import Path
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlencode
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent      # 어느 폴더에서 실행해도 static 폴더를 찾도록
DEFAULT_NAVER_ID = "ls2otv9r78"      # 환경변수 NAVER_CLIENT_ID가 없을 때 쓰는 기본 클라이언트 ID

GBIS_BASE = "https://apis.data.go.kr/6410000"
URL_AROUND = f"{GBIS_BASE}/busstationservice/v2/getBusStationAroundListv2"
URL_ARRIVAL = f"{GBIS_BASE}/busarrivalservice/v2/getBusArrivalListv2"
URL_ROUTE_STATIONS = f"{GBIS_BASE}/busrouteservice/v2/getBusRouteStationListv2"   # 노선 경유 정류소
URL_ROUTE_LINE = f"{GBIS_BASE}/busrouteservice/v2/getBusRouteLineListv2"          # 노선 형상(경로선)
URL_BUS_LOC = f"{GBIS_BASE}/buslocationservice/v2/getBusLocationListv2"           # 노선 버스 실시간 위치


# ---------------------------------------------------------------- 공통 도우미
def gbis_key():
    k = os.getenv("GBIS_KEY", "").strip()
    return unquote(k) if k else ""      # 인코딩 키/디코딩 키 어느 쪽을 넣어도 동작


def call_gbis(url, **params):
    params.update(serviceKey=gbis_key(), format="json")
    with urlopen(f"{url}?{urlencode(params, quote_via=quote)}", timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def find_key(obj, name):
    """응답 JSON 어디에 있든 name 키의 값을 찾는다 (응답 구조가 달라도 견디도록)."""
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        for v in obj.values():
            r = find_key(v, name)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, name)
            if r is not None:
                return r
    return None


def as_list(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 실제 API
def real_stations(lat, lng):
    data = call_gbis(URL_AROUND, x=lng, y=lat)       # x=경도, y=위도 (WGS84)
    out = []
    items = find_key(data, "busStationAroundList") or find_key(data, "busStationList")
    for s in as_list(items):
        out.append({"id": "gg:" + str(s["stationId"]), "name": s.get("stationName", ""),
                    "lat": float(s["y"]), "lng": float(s["x"])})
    return out


RADIUS_M = 1000          # 표시할 최대 반경(m)
STEP_M = 700             # 조회 지점 간격(m). API 1회 조회 반경이 약 500m라서 여러 지점을 조회해 합침
_cache = {}              # {(위도,경도): (시각, 결과)}  → API 호출 횟수 절약


def _meters(lat, lng, lat2, lng2):
    dy = (lat2 - lat) * 111320
    dx = (lng2 - lng) * 111320 * math.cos(math.radians(lat))
    return math.hypot(dx, dy)


def real_stations_wide(lat, lng):
    key = (round(lat, 3), round(lng, 3))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    dlat = STEP_M / 111320
    dlng = STEP_M / (111320 * math.cos(math.radians(lat)))
    points = [(lat, lng), (lat + dlat, lng), (lat - dlat, lng), (lat, lng + dlng), (lat, lng - dlng)]

    def safe(p):
        try:
            return real_stations(*p)
        except Exception:
            if p == points[0]:      # 중심 지점 실패는 그대로 에러 처리
                raise
            return []

    with ThreadPoolExecutor(5) as ex:
        results = list(ex.map(safe, points))
    merged = {}
    for lst in results:
        for s in lst:
            merged[s["id"]] = s
    out = [s for s in merged.values() if _meters(lat, lng, s["lat"], s["lng"]) <= RADIUS_M]
    out.sort(key=lambda s: _meters(lat, lng, s["lat"], s["lng"]))
    _cache[key] = (time.time(), out)
    return out


def real_arrivals(station_id):
    data = call_gbis(URL_ARRIVAL, stationId=station_id)
    items = find_key(data, "busArrivalList") or find_key(data, "busArrivalItem")
    groups = {}                                           # 노선번호 → [도착까지 초, ...]
    for b in as_list(items):
        if str(b.get("flag", "")).upper() == "STOP":      # 운행종료
            continue
        name = str(b.get("routeName") or b.get("routeId"))
        dest = str(b.get("routeDestName") or "")          # 진행방향 마지막 정류소명
        meta = groups.setdefault((name, dest), {"times": [], "route_id": "gg:" + str(b.get("routeId") or ""),
                                                "sta_order": to_int(b.get("staOrder"))})
        for n in ("1", "2"):                              # 첫 번째/두 번째 차량
            sec = to_int(b.get(f"predictTimeSec{n}"))
            if sec is None:
                mins = to_int(b.get(f"predictTime{n}"))   # 분 단위 필드로 대체
                sec = mins * 60 if mins is not None else None
            if sec is not None and sec > 0:
                meta["times"].append(sec)
    out = [{"route": r, "dest": d, "times": sorted(g["times"]), "route_id": g["route_id"], "sta_order": g["sta_order"]}
           for (r, d), g in groups.items() if g["times"]]
    return sorted(out, key=lambda x: x["times"][0])


# ---------------------------------------------------------------- 노선 경로 / 버스 위치
_ttl = {}


def cached(key, ttl, fn):
    hit = _ttl.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _ttl[key] = (time.time(), v)
    return v


def route_stations(route_id):
    data = call_gbis(URL_ROUTE_STATIONS, routeId=route_id)
    out = []
    for s in as_list(find_key(data, "busRouteStationList")):
        out.append({"id": "gg:" + str(s["stationId"]), "name": s.get("stationName", ""),
                    "lat": float(s["y"]), "lng": float(s["x"]), "seq": to_int(s.get("stationSeq")),
                    "turn": str(s.get("turnYn", "")).upper() == "Y"})                  # 회차점 여부
    return sorted(out, key=lambda s: s["seq"] or 0)


def route_line(route_id):
    data = call_gbis(URL_ROUTE_LINE, routeId=route_id)
    pts = []
    for p in as_list(find_key(data, "busRouteLineList")):
        try:
            pts.append([float(p["y"]), float(p["x"])])
        except (KeyError, TypeError, ValueError):
            pass
    return pts


def bus_locations(route_id):
    data = call_gbis(URL_BUS_LOC, routeId=route_id)
    return [{"plate": str(b.get("plateNo", "")), "seq": to_int(b.get("stationSeq")),
             "station_id": str(b.get("stationId", "")), "seats": to_int(b.get("remainSeatCnt")),
             "crowded": to_int(b.get("crowded"))}
            for b in as_list(find_key(data, "busLocationList"))]


@app.get("/api/route/{route_id}")
def route(route_id: str, raw: str = ""):
    """노선 경로선 + 경유 정류소 + 실시간 버스 위치. raw=line|stations|buses 로 원본 응답 확인."""
    if not gbis_key():
        return {"line": [], "stations": [], "buses": [], "errors": ["인증키 없음"]}
    region, _, rid = route_id.partition(":")
    if not rid:
        region, rid = "gg", route_id
    if region == "se":
        fns = {"stations": lambda: seoul_route_stations(rid), "line": lambda: seoul_route_line(rid),
               "buses": lambda: seoul_bus_locations(rid)}
        raws = {"stations": ("busRouteInfo/getStaionByRoute", "busRouteId"), "line": ("busRouteInfo/getRoutePath", "busRouteId"),
                "buses": ("buspos/getBusPosByRtid", "busRouteId")}
        if raw:
            return JSONResponse({"xml": seoul_raw(raws[raw][0], busRouteId=rid)}) if raw in raws else {"error": "raw=line|stations|buses"}
    else:
        fns = {"stations": lambda: route_stations(rid), "line": lambda: route_line(rid), "buses": lambda: bus_locations(rid)}
        if raw:
            url = {"line": URL_ROUTE_LINE, "stations": URL_ROUTE_STATIONS, "buses": URL_BUS_LOC}.get(raw)
            try:
                return call_gbis(url, routeId=rid) if url else {"error": "raw=line|stations|buses"}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=502)
    res = {"line": [], "stations": [], "buses": [], "errors": []}
    for key, ttl in (("stations", 86400), ("line", 86400), ("buses", 15)):
        try:
            res[key] = cached((key, route_id), ttl, fns[key])
        except Exception as e:
            res["errors"].append(f"{key}: {e}")
    return res


# ---------------------------------------------------------------- 지하철 (서울 열린데이터광장 실시간 + TMAP 장소검색)
SUBWAY_BASE = "http://swopenAPI.seoul.go.kr/api/subway"
URL_TMAP_POI = "https://apis.openapi.sk.com/tmap/pois"
METRO_BOX = (36.9, 38.1, 126.4, 127.8)          # 수도권 대략 범위 (이 밖에서는 지하철 조회를 하지 않음)
KST = timezone(timedelta(hours=9))
SUBWAY_LINES = {                                # subwayId → (이름, 색)
    "1001": ("1호선", "#0052A4"), "1002": ("2호선", "#00A84D"), "1003": ("3호선", "#EF7C1C"), "1004": ("4호선", "#00A5DE"),
    "1005": ("5호선", "#996CAC"), "1006": ("6호선", "#CD7C2F"), "1007": ("7호선", "#747F00"), "1008": ("8호선", "#E6186C"),
    "1009": ("9호선", "#BDB092"), "1063": ("경의중앙선", "#77C4A3"), "1065": ("공항철도", "#0090D2"), "1067": ("경춘선", "#0C8E72"),
    "1075": ("수인분당선", "#F5A200"), "1077": ("신분당선", "#D4003B"), "1092": ("우이신설선", "#B0CE18"),
}


def subway_key():
    return os.getenv("SEOUL_SUBWAY_KEY", "").strip()


def in_metro(lat, lng):
    return METRO_BOX[0] <= lat <= METRO_BOX[1] and METRO_BOX[2] <= lng <= METRO_BOX[3]


def tmap_get(url, **params):
    req = Request(f"{url}?{urlencode(params, quote_via=quote)}",
                  headers={"appKey": tmap_key(), "Accept": "application/json"})
    with urlopen(req, timeout=8) as r:
        body = r.read().decode("utf-8").strip()
    return json.loads(body) if body else {}


def subway_poi(lat, lng):
    return tmap_get(URL_TMAP_POI, version=1, searchKeyword="지하철역", searchType="all", searchtypCd="R",
                    centerLat=lat, centerLon=lng, radius=1, count=20, reqCoordType="WGS84GEO", resCoordType="WGS84GEO")


def subway_stations(lat, lng):
    """내 주변 지하철역 (TMAP 장소검색). 노선별로 나뉜 결과를 역 이름 기준으로 합친다."""
    out = {}
    for p in as_list(find_key(subway_poi(lat, lng), "poi")):
        name = str(p.get("name", ""))
        biz = f'{p.get("upperBizName", "")}{p.get("middleBizName", "")}{p.get("lowerBizName", "")}'
        if not ("지하철" in biz or "전철" in biz or re.search(r"역\s*\S*(호선|선|철도)$", name)):
            continue
        base = re.sub(r"역(\s.*)?$", "", name).strip()          # "사당역 2호선" → "사당" (실시간 API가 쓰는 역 이름)
        try:
            la, lo = float(p.get("noorLat") or p.get("frontLat")), float(p.get("noorLon") or p.get("frontLon"))
        except (TypeError, ValueError):
            continue
        if base and base not in out:
            out[base] = {"id": "sw:" + base, "name": base + "역", "lat": la, "lng": lo, "kind": "subway"}
    return list(out.values())


def subway_arrivals(station):
    url = f"{SUBWAY_BASE}/{subway_key()}/json/realtimeStationArrival/0/60/{quote(station)}"
    with urlopen(url, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8"))
    items = data.get("realtimeArrivalList")
    if items is None:
        code = (data.get("errorMessage") or {}).get("code") or data.get("code")
        if code in (None, "INFO-200"):                 # INFO-200: 해당하는 데이터가 없음
            return []
        raise ValueError(f"{code}: {(data.get('errorMessage') or data).get('message')}")
    now = datetime.now(KST).replace(tzinfo=None)
    groups = {}
    for it in items:
        if str(it.get("arvlCd")) == "2":               # 이미 출발한 열차
            continue
        try:
            age = max(0.0, (now - datetime.strptime(it["recptnDt"], "%Y-%m-%d %H:%M:%S")).total_seconds())
        except (KeyError, ValueError):
            age = 0.0
        if age > 300:                                  # 5분 넘게 갱신 안 된 오래된 정보는 제외
            continue
        sec = to_int(it.get("barvlDt"))
        if sec and sec > 0:
            sec = max(1, int(sec - age))               # 정보를 받은 뒤 지난 시간만큼 보정
        elif str(it.get("arvlCd")) in ("0", "1"):      # 진입/도착 상태
            sec = 20
        else:
            continue
        sid, ud = str(it.get("subwayId")), str(it.get("updnLine", ""))
        label, color = SUBWAY_LINES.get(sid, (f"지하철{sid}", "#6b7280"))
        tl = str(it.get("trainLineNm", ""))
        g = groups.setdefault((sid, ud), {"route": f"{label} {ud}", "dest": tl.split(" - ")[-1] if tl else "",
                                          "times": [], "route_id": "", "sta_order": None, "kind": "subway", "color": color})
        g["times"].append(sec)
    out = [dict(g, times=sorted(g["times"])[:2]) for g in groups.values() if g["times"]]
    return sorted(out, key=lambda x: (x["route"]))


@app.get("/api/subway/stations")
def subway_stations_api(lat: float, lng: float, raw: int = 0):
    if not (subway_key() and tmap_key()):
        return JSONResponse({"error": "SEOUL_SUBWAY_KEY와 TMAP_KEY가 필요해요"}, status_code=400)
    try:
        return subway_poi(lat, lng) if raw else subway_stations(lat, lng)
    except Exception as e:
        return JSONResponse({"error": f"지하철역 조회 실패: {e}"}, status_code=502)


# ---------------------------------------------------------------- 도보 경로 (TMAP 보행자 경로안내)
URL_TMAP_WALK = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1"


def tmap_key():
    return os.getenv("TMAP_KEY", "").strip()


def tmap_call(slat, slng, elat, elng):
    body = json.dumps({
        "startX": slng, "startY": slat, "endX": elng, "endY": elat,          # x=경도, y=위도
        "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
        "startName": quote("내 위치"), "endName": quote("정류장"),
    }).encode()
    req = Request(URL_TMAP_WALK, data=body, method="POST",
                  headers={"appKey": tmap_key(), "Content-Type": "application/json", "Accept": "application/json"})
    with urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def estimate_walk(slat, slng, elat, elng):
    meters = _meters(slat, slng, elat, elng) * 1.3
    return {"sec": meters / 1.25, "meters": meters, "path": [[slat, slng], [elat, elng]], "estimated": True}


def tmap_walk(slat, slng, elat, elng):
    data = tmap_call(slat, slng, elat, elng)
    sec = meters = None
    path = []
    for f in data.get("features", []):
        props, geom = f.get("properties", {}), f.get("geometry", {})
        if sec is None and "totalTime" in props:
            sec, meters = props["totalTime"], props.get("totalDistance")
        if geom.get("type") == "LineString":
            path += [[c[1], c[0]] for c in geom["coordinates"]]               # [경도,위도] → [위도,경도]
    if sec is None:
        raise ValueError("응답에 totalTime이 없어요")
    return {"sec": float(sec), "meters": float(meters or 0), "path": path, "estimated": False}


@app.get("/api/walk")
def walk(slat: float, slng: float, elat: float, elng: float, raw: int = 0):
    if raw and tmap_key():
        try:
            return tmap_call(slat, slng, elat, elng)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)
    if not tmap_key():
        return estimate_walk(slat, slng, elat, elng)
    key = ("walk", round(slat, 4), round(slng, 4), round(elat, 4), round(elng, 4))
    try:
        return cached(key, 300, lambda: tmap_walk(slat, slng, elat, elng))      # 5분만 저장 (TMAP 약관: 24시간 초과 저장 금지)
    except Exception as e:
        res = estimate_walk(slat, slng, elat, elng)
        res["error"] = f"TMAP 실패, 직선거리 추정으로 대체: {e}"
        return res


# ---------------------------------------------------------------- 샘플 데이터
NAMES = ["시청앞", "중앙시장", "역전광장", "구청입구", "공원앞", "대학교정문", "우체국앞", "초등학교앞"]
ROUTES = ["1", "5-1", "21", "33", "101", "9707", "M7412"]


def mock_stations(lat, lng):
    rnd = random.Random(f"{lat:.3f},{lng:.3f}")
    out = []
    for i, name in enumerate(NAMES):
        ang, dist = rnd.uniform(0, 2 * math.pi), rnd.uniform(80, 600)
        out.append({"id": f"S{i}", "name": name,
                    "lat": lat + dist * math.cos(ang) / 111320,
                    "lng": lng + dist * math.sin(ang) / (111320 * math.cos(math.radians(lat)))})
    return out


def mock_arrivals(station_id):
    rnd = random.Random(f"{station_id}{int(time.time() // 120)}")
    out = [{"route": r, "dest": rnd.choice(["수원역", "사당역", "강남역", "광교중앙역"]), "route_id": "", "sta_order": None,
            "times": sorted([rnd.randint(90, 900), rnd.randint(900, 1800)])}
           for r in rnd.sample(ROUTES, 4)]
    return sorted(out, key=lambda b: b["times"][0])


# ---------------------------------------------------------------- 서울시 (ws.bus.go.kr)
SEOUL_BASE = "http://ws.bus.go.kr/api/rest"
SEOUL_BOX = (37.36, 37.76, 126.72, 127.23)       # 서울 + 약간의 여유 (위도 최소·최대, 경도 최소·최대)


def seoul_key():
    k = (os.getenv("SEOUL_KEY") or os.getenv("GBIS_KEY") or "").strip()
    return unquote(k) if k else ""


def in_seoul(lat, lng):
    return SEOUL_BOX[0] <= lat <= SEOUL_BOX[1] and SEOUL_BOX[2] <= lng <= SEOUL_BOX[3]


def seoul_items(path, **params):
    """서울시 API 호출 → itemList 항목을 dict 목록으로. (XML 기본, JSON이 와도 처리)"""
    params["serviceKey"] = seoul_key()
    with urlopen(f"{SEOUL_BASE}/{path}?{urlencode(params, quote_via=quote)}", timeout=8) as r:
        text = r.read().decode("utf-8").strip()
    if text.startswith("{"):
        data = json.loads(text)
        code = str(find_key(data, "headerCd"))
        if code not in ("0", "4", "None"):
            raise ValueError(f"서울시 API 오류 {code}: {find_key(data, 'headerMsg')}")
        return [x for x in as_list(find_key(data, "itemList")) if isinstance(x, dict)]
    root = ET.fromstring(text)
    code = (root.findtext(".//headerCd") or "0").strip()
    if code == "4":                                    # 4 = 결과 없음
        return []
    if code != "0":
        raise ValueError(f"서울시 API 오류 {code}: {root.findtext('.//headerMsg')}")
    return [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("itemList")]


def seoul_raw(path, **params):
    params["serviceKey"] = seoul_key()
    with urlopen(f"{SEOUL_BASE}/{path}?{urlencode(params, quote_via=quote)}", timeout=8) as r:
        return r.read().decode("utf-8")


def seoul_stations(lat, lng):
    items = seoul_items("stationinfo/getStationByPos", tmX=lng, tmY=lat, radius=RADIUS_M)   # tmX=경도, tmY=위도 (WGS84)
    out = []
    for s in items:
        try:
            x, y = s.get("gpsX") or s.get("tmX"), s.get("gpsY") or s.get("tmY")
            ars = s.get("arsId", "")
            if not ars or ars == "0":                  # 정류소 번호가 없는 곳은 도착정보 조회가 안 됨
                continue
            out.append({"id": "se:" + ars, "name": s.get("stationNm", ""), "lat": float(y), "lng": float(x)})
        except (TypeError, ValueError):
            continue
    return out


def seoul_msg_sec(msg):
    """'3분5초후[2번째 전]' 같은 메시지에서 도착까지 초를 뽑는다."""
    if not msg:
        return None
    if "곧" in msg:
        return 30
    m = re.search(r"(?:(\d+)분)?\s*(?:(\d+)초)?\s*후", msg)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return None


def seoul_arrivals(station_id):
    ars = station_id.split(":", 1)[1]
    groups = {}
    for b in seoul_items("stationinfo/getStationByUid", arsId=ars):
        name = b.get("rtNm") or b.get("busRouteAbrv") or b.get("busRouteId")
        dest = b.get("adirection", "")
        rid = b.get("busRouteId", "")
        meta = groups.setdefault((name, dest), {"times": [], "route_id": "se:" + rid, "sta_order": to_int(b.get("staOrd"))})
        for n in ("1", "2"):
            sec = to_int(b.get(f"exps{n}"))                 # 예상 도착 시간(초)
            if not sec or sec <= 0:
                sec = to_int(b.get(f"traTime{n}"))
            if not sec or sec <= 0:
                sec = seoul_msg_sec(b.get(f"arrmsg{n}"))     # 마지막 수단: 도착 메시지 해석
            if sec and sec > 0:
                meta["times"].append(sec)
    out = [{"route": r, "dest": d, "times": sorted(g["times"]), "route_id": g["route_id"], "sta_order": g["sta_order"]}
           for (r, d), g in groups.items() if g["times"]]
    return sorted(out, key=lambda x: x["times"][0])


def seoul_route_stations(rid):
    out = []
    for s in seoul_items("busRouteInfo/getStaionByRoute", busRouteId=rid):
        try:
            out.append({"id": "se:" + s.get("arsId", ""), "name": s.get("stationNm", ""),
                        "lat": float(s["gpsY"]), "lng": float(s["gpsX"]), "seq": to_int(s.get("seq")),
                        "turn": str(s.get("transYn", "")).upper() == "Y"})              # 회차지 여부
        except (KeyError, ValueError):
            pass
    return sorted(out, key=lambda s: s["seq"] or 0)


def seoul_route_line(rid):
    pts = []
    for p in seoul_items("busRouteInfo/getRoutePath", busRouteId=rid):
        try:
            pts.append([float(p["gpsY"]), float(p["gpsX"])])
        except (KeyError, ValueError):
            pass
    return pts


def seoul_bus_locations(rid):
    out = []
    for b in seoul_items("buspos/getBusPosByRtid", busRouteId=rid):
        try:
            out.append({"plate": b.get("plainNo", ""), "seq": to_int(b.get("sectOrd")),
                        "lat": float(b["gpsY"]), "lng": float(b["gpsX"]),      # 서울은 실제 GPS 좌표 제공
                        "crowded": to_int(b.get("congetion"))})
        except (KeyError, ValueError):
            pass
    return out


# ---------------------------------------------------------------- 엔드포인트
@app.get("/api/stations")
def stations(lat: float, lng: float, raw: int = 0):
    if not gbis_key():
        return mock_stations(lat, lng)
    if raw:
        try:
            return {"gyeonggi": call_gbis(URL_AROUND, x=lng, y=lat),
                    "seoul": seoul_raw("stationinfo/getStationByPos", tmX=lng, tmY=lat, radius=RADIUS_M)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)
    jobs = {"gg": lambda: cached(("st", "gg", round(lat, 3), round(lng, 3)), 600, lambda: real_stations_wide(lat, lng))}
    if seoul_key() and in_seoul(lat, lng):
        jobs["se"] = lambda: cached(("st", "se", round(lat, 3), round(lng, 3)), 600, lambda: seoul_stations(lat, lng))
    if subway_key() and tmap_key() and in_metro(lat, lng):
        jobs["sw"] = lambda: cached(("st", "sw", round(lat, 3), round(lng, 3)), 600, lambda: subway_stations(lat, lng))
    out, errors = [], []
    with ThreadPoolExecutor(3) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
    for k, f in futs.items():
        try:
            out += f.result()
        except Exception as e:
            if k != "sw":                                # 지하철 조회 실패는 버스 정류장 표시를 막지 않음
                errors.append(f"{'경기도' if k == 'gg' else '서울시'}: {e}")
    if not out and errors:
        return JSONResponse({"error": "정류장 조회 실패 · " + " / ".join(errors)}, status_code=502)
    out.sort(key=lambda s: _meters(lat, lng, s["lat"], s["lng"]))
    return out


@app.get("/api/arrivals/{station_id}")
def arrivals(station_id: str, raw: int = 0):
    if station_id.startswith("sw:"):
        if not subway_key():
            return JSONResponse({"error": "SEOUL_SUBWAY_KEY가 설정되지 않았어요"}, status_code=400)
        try:
            return cached(("swarr", station_id), 15, lambda: subway_arrivals(station_id[3:]))
        except Exception as e:
            return JSONResponse({"error": f"지하철 도착정보 조회 실패: {e}"}, status_code=502)
    if not gbis_key():
        return mock_arrivals(station_id)
    try:
        if station_id.startswith("se:"):
            if raw:
                return JSONResponse({"xml": seoul_raw("stationinfo/getStationByUid", arsId=station_id[3:])})
            return seoul_arrivals(station_id)
        sid = station_id.split(":", 1)[-1]
        return call_gbis(URL_ARRIVAL, stationId=sid) if raw else real_arrivals(sid)
    except Exception as e:
        return JSONResponse({"error": f"도착정보 조회 실패: {e}"}, status_code=502)


@app.get("/health")
def health():
    """Render 상태 확인용 (키 설정 여부만 알려주고 값은 노출하지 않음)"""
    return {"ok": True, "gbis": bool(gbis_key()), "tmap": bool(tmap_key()), "subway": bool(subway_key())}


@app.get("/")
def index():
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    nid = os.getenv("NAVER_CLIENT_ID", "").strip() or DEFAULT_NAVER_ID      # 클라이언트 ID는 브라우저에 공개되는 값이라 기본값을 둬도 안전
    print(f"[지도] 네이버 Client ID = {nid}")                                  # 서버 창에서 어떤 ID가 쓰였는지 바로 확인
    return HTMLResponse(html.replace("__NAVER_CLIENT_ID__", nid))


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
