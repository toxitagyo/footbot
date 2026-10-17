"""Foot Bot : python main.py  ->  génère docs/index.html
Peut être relancé souvent (cron toutes les 15 min) : le cache évite de gaspiller les quotas,
et les compos officielles sont récupérées dès H-75 min."""
import json, os, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, FileSystemLoader

import model as M
import sources as S

PRED_FILE = "data/predictions.json"
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def load_preds():
    return json.load(open(PRED_FILE, encoding="utf-8")) if os.path.exists(PRED_FILE) else {}


def resolve(preds, results):
    """Bilan : marque comme gagnés/perdus les pronos dont le match est terminé."""
    for r in results:
        p = preds.get(str(r["id"]))
        if p and not p.get("done"):
            p["hit"] = {t: int(M.predicate(p[t], r["hg"], r["ag"])) for t in ("safe", "medium", "risky")}
            p["done"] = True


def stats(preds):
    s = {t: [0, 0] for t in ("safe", "medium", "risky")}
    for p in preds.values():
        if p.get("done"):
            for t in s:
                s[t][0] += 1; s[t][1] += p["hit"][t]
    return s


def main():
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    fk = os.getenv("API_FOOTBALL_KEY") or cfg["api_football_key"]
    ok = os.getenv("ODDS_API_KEY") or cfg["odds_api_key"]
    ow = os.getenv("OPENWEATHER_KEY") or cfg.get("openweather_key", "")
    if fk.startswith("TA_"):
        sys.exit("Renseigne ta clé API-Football dans config.yaml (ou API_FOOTBALL_KEY).")
    use_odds = not ok.startswith("TA_")
    if not use_odds:
        print("[i] Pas de clé Odds API : la page sera générée sans cotes Winamax.")
    use_weather = bool(ow)
    if not use_weather:
        print("[i] Pas de clé OpenWeather : la météo ne sera pas affichée (optionnelle).")

    api = S.ApiFootball(fk)
    ttl = cfg["ttl_h"]
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(timezone.utc)
    season = now.year if now.month >= 7 else now.year - 1
    d1, d2 = now.date().isoformat(), (now.date() + timedelta(days=cfg["days_ahead"])).isoformat()
    preds = load_preds()
    matches = []

    # Pré-chargement / optimisation des cotes :
    # Si toutes les ligues ont un cache frais, on lira les caches plutôt que de lancer des requêtes.
    events_cache = {}
    if use_odds:
        all_fresh = True
        for lg in cfg["leagues"]:
            key = f"odds:{lg['odds_key']}"
            if not S.cache_fresh(key, ttl["odds"]):
                all_fresh = False
                break
        if all_fresh:
            for lg in cfg["leagues"]:
                key = f"odds:{lg['odds_key']}"
                events_cache[lg["odds_key"]] = S.read_cache(key) or []
            print("[i] Tous les caches odds sont frais — utilisation directe des caches sans appels réseau.")

    for lg in cfg["leagues"]:
        print(f"== {lg['name']}")
        fx = [f for f in api.fixtures(lg["api_id"], season, d1, d2, ttl["fixtures"]) if f["status"] in ("NS", "TBD")]
        results = api.results(lg["api_id"], season, ttl["results"])
        resolve(preds, results)
        if not fx:
            continue
        st, tb = M.strengths(results, now), M.table(results)
        # récupérer events soit depuis le cache préchargé, soit via la fonction (qui utilisera le cache si valide)
        if use_odds and lg["odds_key"] in events_cache:
            events = events_cache[lg["odds_key"]]
        else:
            events = S.fetch_odds(ok, lg["odds_key"], cfg["odds_markets"], ttl["odds"]) if use_odds else []

        inj_by_fid = {}
        dates = {M.parse_dt(f["date"]).date().isoformat() for f in fx
                 if (M.parse_dt(f["date"]) - now).total_seconds() / 3600 <= cfg["injuries_horizon_h"]}
        for d in sorted(dates):
            for i in api.injuries(lg["api_id"], season, d, ttl["injuries"]):
                inj_by_fid.setdefault(i["fid"], []).append(i)

        for f in fx:
            ko = M.parse_dt(f["date"])
            mins = (ko - now).total_seconds() / 60
            inj = inj_by_fid.get(f["id"], [])
            side = lambda team: [i for i in inj if i["team"] == team]
            n = lambda team: sum(1 if i["type"] == "Missing Fixture" else .5 for i in side(team))
            lineups = api.lineups(f["id"]) if -15 <= mins <= cfg["lineup_window_min"] else []
            odds = S.match_odds(events, f["home"], f["away"]) if events else {}
            lh, la, mat = M.predict(st, tb, f["home"], f["away"], n(f["home"]), n(f["away"]))
            picks = M.make_picks(mat, f["home"], f["away"], odds)
            best = max(((i, j) for i in range(7) for j in range(7)), key=lambda ij: mat[ij[0]][ij[1]])
            local = ko.astimezone(tz)
            fmt_inj = lambda team: [f'{i["player"]} ({i["reason"]})' for i in side(team)]

            fid = str(f["id"])
            if not preds.get(fid, {}).get("done") and mins > 0:
                preds[fid] = {"ko": f["date"], **{t: picks[t]["code"] for t in picks}, "done": False}

            # H2H (dernières confrontations)
            h2 = M.h2h(results, f["home"], f["away"], n=6)

            # météo (OneCall) : on récupère 3 jours (liste) si disponible
            weather = S.fetch_weather(f.get("city"), ow, ttl.get("weather", 3), days=3) if use_weather and f.get("city") else None

            matches.append({
                "ts": ko.timestamp(), "league": lg["name"], "country": lg["country"],
                "date": f"{JOURS[local.weekday()]} {local:%d/%m}", "iso": local.date().isoformat(),
                "time": f"{local:%H:%M}", "venue": f["venue"], "city": f["city"], "referee": f["referee"],
                "round": f["round"], "home": f["home"], "away": f["away"],
                "hi": tb.get(f["home"]), "ai": tb.get(f["away"]),
                "hinj": fmt_inj(f["home"]), "ainj": fmt_inj(f["away"]),
                "hnews": S.team_news(f["home"], ttl["news"]), "anews": S.team_news(f["away"], ttl["news"]),
                "lineups": lineups, "odds": odds,
                "h2h": h2,
                "weather": weather,
                "pr": {"p1": sum(mat[i][j] for i in range(11) for j in range(11) if i > j),
                       "px": sum(mat[i][i] for i in range(11)),
                       "p2": sum(mat[i][j] for i in range(11) for j in range(11) if i < j),
                       "o25": M.prob(mat, "O2.5"), "btts": M.prob(mat, "BTTS_Y"),
                       "lh": lh, "la": la, "score": f"{best[0]}-{best[1]}"},
                "picks": picks,
            })

    matches.sort(key=lambda m: m["ts"])
    # Combiné prudent : les 3 doubles chances les plus sûres des prochaines 48 h
    soon = [m for m in matches if m["ts"] - now.timestamp() < 48 * 3600]
    legs = sorted(soon, key=lambda m: -m["picks"]["safe"]["prob"])[:3]
    combo = None
    if len(legs) == 3:
        p, o = 1.0, 1.0
        for m in legs:
            p *= m["picks"]["safe"]["prob"]
            o = o * m["picks"]["safe"]["odd"] if o and m["picks"]["safe"]["odd"] else None
        combo = {"legs": legs, "prob": p, "odd": round(o, 2) if o else None}

    os.makedirs("docs", exist_ok=True)
    json.dump(preds, open(PRED_FILE, "w", encoding="utf-8"))
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.filters["pct"] = lambda x: f"{x * 100:.0f} %"
    html = env.get_template("index.html.j2").render(
        matches=matches, combo=combo, stats=stats(preds), use_odds=use_odds,
        leagues=[l["name"] for l in cfg["leagues"]], dates=sorted({m["iso"] for m in matches}),
        updated=now.astimezone(tz).strftime("%d/%m/%Y %H:%M"))
    open("docs/index.html", "w", encoding="utf-8").write(html)
    print(f"-> docs/index.html ({len(matches)} matchs)")


if __name__ == "__main__":
    main()