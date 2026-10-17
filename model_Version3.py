"""Modèle de Poisson + génération des 3 niveaux de pronos."""
import math
from collections import defaultdict
from datetime import datetime

MAXG = 10


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pois(l, k):
    return math.exp(-l) * l ** k / math.factorial(k)


# ---------- Force des équipes ----------
def strengths(results, now, half_life=75, k=6):
    """Attaque/défense domicile/extérieur, matchs récents pondérés, ramenés vers la moyenne (k)."""
    if len(results) < 10:
        return {"mh": 1.45, "ma": 1.15, "teams": {}}
    sw = swh = swa = 0.0
    acc = defaultdict(lambda: defaultdict(float))
    for r in results:
        w = 0.5 ** (max((now - parse_dt(r["date"])).days, 0) / half_life)
        sw += w; swh += w * r["hg"]; swa += w * r["ag"]
        h = acc[r["home"]]; h["wh"] += w; h["gfh"] += w * r["hg"]; h["gah"] += w * r["ag"]
        a = acc[r["away"]]; a["wa"] += w; a["gfa"] += w * r["ag"]; a["gaa"] += w * r["hg"]
    mh, ma = swh / sw, swa / sw
    teams = {}
    for t, x in acc.items():
        teams[t] = {"att_h": (x["gfh"] + k * mh) / (x["wh"] + k) / mh,
                    "def_h": (x["gah"] + k * ma) / (x["wh"] + k) / ma,
                    "att_a": (x["gfa"] + k * ma) / (x["wa"] + k) / ma,
                    "def_a": (x["gaa"] + k * mh) / (x["wa"] + k) / mh}
    return {"mh": mh, "ma": ma, "teams": teams}


def table(results):
    t = defaultdict(lambda: {"p": 0, "pts": 0, "gf": 0, "ga": 0, "form": []})
    for r in sorted(results, key=lambda r: r["date"]):
        for team, gf, ga in ((r["home"], r["hg"], r["ag"]), (r["away"], r["ag"], r["hg"])):
            x = t[team]; x["p"] += 1; x["gf"] += gf; x["ga"] += ga
            res = "W" if gf > ga else "D" if gf == ga else "L"
            x["pts"] += 3 if res == "W" else 1 if res == "D" else 0
            x["form"].append(res)
    order = sorted(t, key=lambda n: (-t[n]["pts"], -(t[n]["gf"] - t[n]["ga"]), -t[n]["gf"]))
    for i, n in enumerate(order, 1):
        t[n]["rank"] = i
    return dict(t)


def form_factor(tb, team):
    x = tb.get(team)
    if not x or x["p"] < 5:
        return 1.0
    pts = lambda f: sum(3 if c == "W" else 1 if c == "D" else 0 for c in f)
    d = pts(x["form"][-5:]) / 5 - x["pts"] / x["p"]
    return 1 + 0.04 * max(-1, min(1, d))


# ---------- H2H (confrontations directes) ----------
def h2h(results, home, away, n=6):
    """Retourne les n dernières confrontations entre home et away tirées des résultats.
    Format: {'matches': [{'date':..., 'home':..., 'away':..., 'hg':..., 'ag':...}], 'summary': {...}}"""
    relevant = [r for r in results if (r["home"] == home and r["away"] == away) or (r["home"] == away and r["away"] == home)]
    relevant.sort(key=lambda r: parse_dt(r["date"]), reverse=True)
    last = relevant[:n]
    h_w = a_w = d = 0
    gh = ga = 0
    for r in last:
        gh += r["hg"]
        ga += r["ag"]
        if r["hg"] > r["ag"]:
            if r["home"] == home:
                h_w += 1
            else:
                a_w += 1
        elif r["hg"] < r["ag"]:
            if r["away"] == home:
                h_w += 1
            else:
                a_w += 1
        else:
            d += 1
    cnt = len(last) or 1
    summary = {"played": len(last), "home_w": h_w, "away_w": a_w, "draws": d,
               "avg_home_goals": round(gh/cnt, 2), "avg_away_goals": round(ga/cnt, 2)}
    return {"matches": last, "summary": summary}


# ---------- Prédiction ----------
def predict(st, tb, home, away, abs_h=0, abs_a=0):
    lh, la = st["mh"], st["ma"]
    s = st["teams"]
    if home in s and away in s:
        lh = st["mh"] * s[home]["att_h"] * s[away]["def_a"]
        la = st["ma"] * s[away]["att_a"] * s[home]["def_h"]
    lh *= form_factor(tb, home); la *= form_factor(tb, away)
    lh *= (1 - min(.02 * abs_h, .10)) * (1 + min(.01 * abs_a, .05))
    la *= (1 - min(.02 * abs_a, .10)) * (1 + min(.01 * abs_h, .05))
    M = [[pois(lh, i) * pois(la, j) for j in range(MAXG + 1)] for i in range(MAXG + 1)]
    tot = sum(map(sum, M))
    M = [[v / tot for v in row] for row in M]
    return lh, la, M


def predicate(code, i, j):
    """Vrai si le pari `code` est gagné avec le score i-j (sert au calcul ET au bilan)."""
    if "&" in code:
        return all(predicate(c, i, j) for c in code.split("&"))
    t = i + j
    if code == "1": return i > j
    if code == "X": return i == j
    if code == "2": return i < j
    if code == "1X": return i >= j
    if code == "X2": return i <= j
    if code == "12": return i != j
    if code == "1h2": return i - j >= 2
    if code == "2h2": return j - i >= 2
    if code == "BTTS_Y": return i > 0 and j > 0
    if code == "BTTS_N": return i == 0 or j == 0
    if code[0] == "O": return t > float(code[1:])
    if code[0] == "U": return t < float(code[1:])
    if code.startswith("S:"):
        a, b = code[2:].split("-"); return (i, j) == (int(a), int(b))
    raise ValueError(code)


def prob(M, code):
    return sum(M[i][j] for i in range(MAXG + 1) for j in range(MAXG + 1) if predicate(code, i, j))


def label(code, h, a):
    if "&" in code:
        return " + ".join(label(c, h, a) for c in code.split("&"))
    fixed = {"1": f"Victoire {h}", "2": f"Victoire {a}", "X": "Match nul", "1X": f"{h} ou nul",
             "X2": f"Nul ou {a}", "12": "Pas de match nul", "1h2": f"{h} gagne par 2 buts d'écart ou +",
             "2h2": f"{a} gagne par 2 buts d'écart ou +", "BTTS_Y": "Les deux équipes marquent",
             "BTTS_N": "Une des équipes ne marque pas"}
    if code in fixed: return fixed[code]
    if code[0] == "O": return f"Plus de {code[1:].replace('.', ',')} buts"
    if code[0] == "U": return f"Moins de {code[1:].replace('.', ',')} buts"
    if code.startswith("S:"): return f"Score exact {h} {code[2:].replace('-', ' - ')} {a}"
    return code


# ---------- Cotes ----------
def devig(odds):
    if not all(k in odds for k in "1X2"):
        return {}
    inv = {k: 1 / odds[k] for k in "1X2"}
    s = sum(inv.values())
    return {k: v / s for k, v in inv.items()}


def odd_for(code, odds):
    if code in odds:
        return odds[code], False
    dc = {"1X": ("1", "X"), "X2": ("X", "2"), "12": ("1", "2")}
    if code in dc and all(k in odds for k in "1X2"):
        a, b = dc[code]
        return round(1 / (1 / odds[a] + 1 / odds[b]), 2), True
    return None, False


def make_picks(M, home, away, odds):
    dv = devig(odds)

    def build(code):
        p = prob(M, code); o, est = odd_for(code, odds)
        return {"code": code, "label": label(code, home, away), "prob": round(p, 3), "odd": o, "est": est,
                "value": code in dv and p - dv[code] >= 0.05}

    def choose(cands, pmin, pmax):
        ok = [c for c in cands if pmin <= c["prob"] <= pmax] or cands
        return max(ok, key=lambda c: (c["prob"] * (c["odd"] or 0.93 / max(c["prob"], 1e-9)), c["prob"]))

    safe = max((build(c) for c in ("1X", "X2")), key=lambda c: c["prob"])
    safe["solid"] = safe["prob"] >= 0.75
    medium = choose([build(c) for c in ("1", "2", "O2.5", "U2.5", "BTTS_Y", "BTTS_N")], 0.50, 0.80)
    best = max(((i, j) for i in range(MAXG + 1) for j in range(MAXG + 1)), key=lambda ij: M[ij[0]][ij[1]])
    rc = ["X", "1", "2", "1h2", "2h2", "1&O3.5", "2&O3.5", f"S:{best[0]}-{best[1]}"]
    risky = choose([build(c) for c in rc if c != medium["code"]], 0.06, 0.38)
    return {"safe": safe, "medium": medium, "risky": risky}