#!/usr/bin/env python3
"""Script léger : parcourt les fixtures à venir et force l'actualisation des compos (API-Football).
Utilisé par le workflow 'lineups' toutes les 15 minutes pour ne pas lancer une génération complète.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import yaml

import sources as S

def main():
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    fk = os.getenv("API_FOOTBALL_KEY") or cfg["api_football_key"]
    if fk.startswith("TA_"):
        print("API_FOOTBALL_KEY manquante — abort.")
        sys.exit(0)

    api = S.ApiFootball(fk)
    now = datetime.now(timezone.utc)
    season = now.year if now.month >= 7 else now.year - 1
    d1, d2 = now.date().isoformat(), (now.date() + timedelta(days=cfg["days_ahead"])).isoformat()
    ttl = cfg["ttl_h"]
    total = 0
    updated = 0

    for lg in cfg["leagues"]:
        print(f"== Refresh lineups for {lg['name']}")
        fx = api.fixtures(lg["api_id"], season, d1, d2, ttl["fixtures"])
        if not fx:
            continue
        for f in fx:
            ko = datetime.fromisoformat(f["date"].replace("Z", "+00:00"))
            mins = (ko - now).total_seconds() / 60
            if -15 <= mins <= cfg["lineup_window_min"]:
                total += 1
                try:
                    lu = api.lineups(f["id"])
                    if lu:
                        updated += 1
                except Exception as e:
                    print(f"[!] erreur lineups {f['id']}: {e}")

    print(f"Finished: checked {total} fixtures, refreshed {updated} lineups.")

if __name__ == "__main__":
    main()