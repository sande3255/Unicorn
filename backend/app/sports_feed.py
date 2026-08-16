"""Live MLB game state, used to auto-generate and auto-resolve short-clock
"will they score this half-inning" markets — the sports counterpart to
price_feed.py's crypto/stock/weather feeds.

Uses the MLB Stats API (statsapi.mlb.com): free, no API key, official MLB
data, and it already ships live linescore/play state on every request — no
scraping or unofficial endpoint involved. Same only-the-standard-library
approach as price_feed.py (urllib, no `requests` dependency).

Unlike the price feeds above, a half-inning isn't a single number to
compare against a strike price — it's a "did this specific thing happen
yet" question, so this module returns run counts and an explicit concluded
flag rather than a bare price, and scheduler.py's sports_tick() applies its
own resolve-early-on-a-score / resolve-on-conclusion logic around that
instead of reusing the strike/settlement comparison in scheduler.tick().
"""
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; UnicornDemo/1.0)"
TIMEOUT_SECONDS = 10

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}&hydrate=linescore,team"
FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"


class SportsFeedError(Exception):
    pass


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise SportsFeedError(f"Network error fetching {url}: {e}") from e
    except json.JSONDecodeError as e:
        raise SportsFeedError(f"Unexpected response shape from {url}: {e}") from e


def get_live_games():
    """Returns a list of dicts, one per MLB game currently in progress
    today (UTC date — good enough for a play-money demo; doesn't bother
    correcting for the rare game that starts just before/after UTC
    midnight local time):
      {game_pk, home_name, away_name, inning, half ('Top'/'Bottom')}
    """
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    data = _fetch_json(SCHEDULE_URL.format(date=today))
    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Live":
                continue
            linescore = g.get("linescore") or {}
            inning = linescore.get("currentInning")
            if inning is None:
                continue
            teams = g.get("teams", {})
            try:
                home_name = teams["home"]["team"]["name"]
                away_name = teams["away"]["team"]["name"]
            except (KeyError, TypeError):
                continue
            games.append({
                "game_pk": g["gamePk"],
                "home_name": home_name,
                "away_name": away_name,
                "inning": inning,
                "half": "Top" if linescore.get("isTopInning") else "Bottom",
            })
    return games


def get_inning_status(game_pk, inning: int, half: str):
    """Returns {"runs": int, "concluded": bool} for the batting team in the
    given half-inning of the given game, or None if that half-inning hasn't
    started yet / isn't in the feed. `runs` updates live (mid-half-inning,
    not just once it's over) — callers should treat runs > 0 as an
    immediately-final YES (a team that has already scored can't un-score),
    and wait for concluded=True to resolve a still-scoreless half as NO."""
    data = _fetch_json(FEED_URL.format(game_pk=game_pk))
    linescore = data.get("liveData", {}).get("linescore", {})
    innings = linescore.get("innings", [])
    if inning < 1 or inning > len(innings):
        return None
    entry = innings[inning - 1]
    side_key = "away" if half == "Top" else "home"
    side = entry.get(side_key)
    if side is None or side.get("runs") is None:
        return None

    current_inning = linescore.get("currentInning")
    current_half = "Top" if linescore.get("isTopInning") else "Bottom"
    game_state = data.get("gameData", {}).get("status", {}).get("abstractGameState")
    is_current_half = (game_state == "Live" and current_inning == inning and current_half == half)
    return {"runs": int(side["runs"]), "concluded": not is_current_half}
