"""Collecte : API-Football (matchs, compos, absents), The Odds API (cotes Winamax FR),
Google News RSS (actus). Tout passe par un cache disque pour économiser les quotas."""
import difflib, hashlib, json, os, re, time, unicodedata
from urllib.parse import quote

import feedparser
import requests
from datetime import datetime, timezone

CACHE_DIR = "data/cache"
os.makedirs(CACHE_DIR, exist_ok=True)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cached(key, ttl_h, fn):
    path = os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl_h * 3600:
        return _read(path)
    try:
        data = fn()
    except Exception as e:
        print(f"[!] {key} : {e}")
        return _read(path) if os.path.exists(path) else None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def cache_fresh(key, ttl_h):
    path = os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json")
    return os.path.exists(path) and (time.time() - os.path.getmtime(path) < ttl_h * 3600)


def read_cache(key):
    path = os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json")
    return _read(path) if os.path.exists(path) else None


class ApiFootball:
    base = "https://v3.football.api-sports.io"

    def __init__(self, key):
        self.h = {"x-apisports-key": key}

    def get(self, path, **params):
        r = requests.get(f"{self.base}/{path}", headers=self.h, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("errors"):
            raise RuntimeError(j["errors"])
        return j["response"]

    def fixtures(self, lg, season, d1, d2, ttl):
        def fn():
            raw = self.get("fixtures", league=lg, season=season, **{"from": d1, "to": d2})
            return [{"id": f["fixture"]["id"], "date": f["fixture"]["date"],
                     "status": f["fixture"]["status"]["short"],
                     "venue": f["fixture"]["venue"]["name"], "city": f["fixture"]["venue"]["city"],
                     "referee": f["fixture"]["referee"], "round": f["league"]["round"],
                     "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"]}
                    for f in raw]
        return cached(f"fx:{lg}:{d1}:{d2}", ttl, fn) or []

    def results(self, lg, season, ttl):
        def fn():
            raw = self.get("fixtures", league=lg, season=season, status="FT")
            return [{"id": f["fixture"]["id"], "date": f["fixture"]["date"],
                     "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
                     "hg": f["goals"]["home"], "ag": f["goals"]["away"]}
                    for f in raw if f["goals"]["home"] is not None]
        return cached(f"res:{lg}:{season}", ttl, fn) or []

    def injuries(self, lg, season, date, ttl):
        def fn():
            raw = self.get("injuries", league=lg, season=season, date=date)
            return [{"fid": i["fixture"]["id"], "team": i["team"]["name"],
                     "player": i["player"]["name"], "type": i["player"]["type"],
                     "reason": i["player"]["reason"]} for i in raw]
        return cached(f"inj:{lg}:{date}", ttl, fn) or []

    def lineups(self, fid):
        def fn():
            raw = self.get("fixtures/lineups", fixture=fid)
            fmt = lambda L: [f'{p["player"]["number"] or ""} {p["player"]["name"]}'.strip() for p in L]
            return [{"team": x["team"]["name"], "formation": x["formation"],
                     "coach": (x.get("coach") or {}).get("name"),
                     "xi": fmt(x["startXI"]), "subs": fmt(x["substitutes"])} for x in raw]
        return cached(f"lu:{fid}", 0.15, fn) or []


def fetch_odds(key, sport, markets, ttl):
    def fn():
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                         params=dict(apiKey=key, bookmakers="winamax_fr", markets=markets,
                                     oddsFormat="decimal"), timeout=30)
        r.raise_for_status()
        return r.json()
    return cached(f"odds:{sport}", ttl, fn) or []


def _norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    stop = {"fc", "afc", "cf", "sc", "ac", "as", "sk", "fk", "bv", "vfb", "vfl", "cd", "ud", "sv", "rc"}
    return " ".join(w for w in s.split() if w not in stop)


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    if a and b and (a in b or b in a):
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def _parse_odds(ev):
    out = {}
    for bk in ev.get("bookmakers", []):
        if bk["key"] != "winamax_fr":
            continue
        for m in bk["markets"]:
            for o in m["outcomes"]:
                if m["key"] == "h2h":
                    k = "X" if o["name"] == "Draw" else "1" if o["name"] == ev["home_team"] else "2"
                    out[k] = o["price"]
                elif m["key"] == "totals":
                    out[("O" if o["name"] == "Over" else "U") + str(o["point"])] = o["price"]
    return out


def match_odds(events, home, away):
    best, bs = None, 0
    for ev in events:
        s = (_sim(home, ev["home_team"]) + _sim(away, ev["away_team"])) / 2
        if s > bs:
            best, bs = ev, s
    return _parse_odds(best) if best and bs >= 0.62 else {}


def team_news(team, ttl):
    def fn():
        url = f"https://news.google.com/rss/search?q={quote(team + ' football')}+when:3d&hl=fr&gl=FR&ceid=FR:fr"
        return [{"title": e.title, "link": e.link} for e in feedparser.parse(url).entries[:3]]
    return cached(f"news:{team}", ttl, fn) or []


def _geocode_city(city, api_key):
    if not city or not api_key:
        return None
    url = "http://api.openweathermap.org/geo/1.0/direct"
    params = {"q": city + ",FR", "limit": 1, "appid": api_key}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    return data[0]["lat"], data[0]["lon"]


def fetch_weather(city, api_key, ttl_h, days=3):
    if not city or not api_key:
        return None

    geo_key = f"geo:{city}"
    def _geo_fn():
        return _geocode_city(city, api_key)
    geo = cached(geo_key, 24, _geo_fn)
    if not geo:
        return None
    lat, lon = geo

    oc_key = f"onecall:{lat:.4f}:{lon:.4f}"

    def _onecall_fn():
        url = "https://api.openweathermap.org/data/2.5/onecall"
        params = {"lat": lat, "lon": lon, "exclude": "minutely,hourly,alerts", "units": "metric", "appid": api_key, "lang": "fr"}
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        return r.json()

    oc = cached(oc_key, ttl_h, _onecall_fn)
    if not oc:
        return None

    daily = oc.get("daily", [])
    if not daily:
        cur = oc.get("current", {})
        return [{"date": None, "temp_day": cur.get("temp"), "temp_night": None,
                 "desc": (cur.get("weather") or [{}])[0].get("description"), "pop": 0.0, "rain": cur.get("rain", 0.0)}]

    out = []
    from datetime import datetime as _dt
    for d in daily[:max(1, min(len(daily), days))]:
        temp_day = d.get("temp", {}).get("day")
        temp_night = d.get("temp", {}).get("night")
        weather = (d.get("weather") or [{}])[0]
        desc = weather.get("description")
        pop = d.get("pop", 0.0)
        rain = d.get("rain", 0.0)
        out.append({"date": _dt.fromtimestamp(d.get("dt", 0), tz=timezone.utc).date().isoformat(),
                    "temp_day": temp_day, "temp_night": temp_night, "desc": desc, "pop": pop, "rain": rain})
    return out