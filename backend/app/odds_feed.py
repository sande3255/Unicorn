"""Real sportsbook moneyline odds, from The Odds API (the-odds-api.com) —
used to seed and resolve "will this team win" markets at real-world implied
probabilities, the same way external_markets.py seeds Kalshi/Polymarket
imports at their real odds.

Unlike every other feed in this app, this one needs a paid/rate-limited API
key (ODDS_API_KEY), so it's written to degrade silently rather than break
anything when that key isn't set: get_upcoming_odds()/get_scores() raise
OddsFeedError, which scheduler.py's odds_tick() catches and logs once, the
same defensive pattern as every other feed's failure handling here.

Only the standard library is used (urllib), matching price_feed.py and
sports_feed.py — no extra dependency for one more HTTP call.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; UnicornDemo/1.0)"
TIMEOUT_SECONDS = 10
BASE_URL = "https://api.the-odds-api.com/v4/sports"

# Read once at import time, not per-call — this key is expected to live in
# an environment variable (ODDS_API_KEY), never hardcoded here or anywhere
# else in this codebase. Locally: export it in your shell (this project
# doesn't use python-dotenv, so a .env file needs to be sourced yourself).
# On Railway: Variables tab on the service, same as UNICORN_ADMIN_PASSWORD.
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

# Four leagues match the rest of this app's "well-known American names"
# curation (see scheduler.py's module docstring) — the sports Americans
# actually follow, not full odds-API sport coverage (which includes dozens
# of leagues worldwide).
SPORT_KEYS = {
    "americanfootball_nfl": "NFL",
    "basketball_nba": "NBA",
    "baseball_mlb": "MLB",
    "icehockey_nhl": "NHL",
}


class OddsFeedError(Exception):
    pass


def _get(sport_key: str, endpoint: str, **params) -> object:
    if not ODDS_API_KEY:
        raise OddsFeedError("ODDS_API_KEY is not set")
    query = urllib.parse.urlencode({**params, "apiKey": ODDS_API_KEY})
    url = f"{BASE_URL}/{sport_key}/{endpoint}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        # HTTPError bodies from this API usually explain quota/auth
        # problems (e.g. "OUT_OF_USAGE_CREDITS") — worth surfacing rather
        # than swallowing, since scheduler.py logs whatever this raises.
        detail = ""
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = f" — {e.read().decode('utf-8')[:200]}"
            except Exception:  # noqa: BLE001
                pass
        raise OddsFeedError(f"Network error fetching {endpoint} for {sport_key}: {e}{detail}") from e
    except json.JSONDecodeError as e:
        raise OddsFeedError(f"Unexpected response shape from {endpoint} for {sport_key}: {e}") from e


def _american_to_prob(price: float) -> float:
    """Converts a single side's American odds to that side's raw implied
    probability — still includes the bookmaker's vig, so two sides' raw
    probabilities sum to a bit over 1.0. Callers normalize (see
    get_upcoming_odds) to get a fair, vig-free probability."""
    if price > 0:
        return 100.0 / (price + 100.0)
    return -price / (-price + 100.0)


def get_upcoming_odds(sport_key: str, hours_ahead: int = 24):
    """Returns a list of upcoming/live games for the given sport (see
    SPORT_KEYS), each:
      {event_id, home_team, away_team, commence_time, home_prob}
    home_prob is the vig-free implied probability the home team wins,
    averaged if multiple US bookmakers are present in the response, or
    None if no bookmaker has posted a moneyline (h2h) line for that game
    yet — callers should skip those rather than open a market at a
    made-up price."""
    data = _get(sport_key, "odds", regions="us", markets="h2h", oddsFormat="american")
    games = []
    for event in data:
        home_team = event.get("home_team")
        away_team = event.get("away_team")
        if not home_team or not away_team:
            continue
        home_probs = []
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = {o["name"]: o["price"] for o in market.get("outcomes", []) if "name" in o and "price" in o}
                if home_team not in outcomes or away_team not in outcomes:
                    continue
                home_raw = _american_to_prob(outcomes[home_team])
                away_raw = _american_to_prob(outcomes[away_team])
                total = home_raw + away_raw
                if total <= 0:
                    continue
                home_probs.append(home_raw / total)  # de-vig, normalized to sum to 1
        if not home_probs:
            continue
        games.append({
            "event_id": event["id"],
            "home_team": home_team,
            "away_team": away_team,
            "commence_time": event.get("commence_time"),
            "home_prob": sum(home_probs) / len(home_probs),
        })
    return games


def get_scores(sport_key: str, days_from: int = 2):
    """Returns completed-game results from the last `days_from` days plus
    any currently live games, keyed by event id:
      {event_id: {"completed": bool, "home_score": int|None, "away_score": int|None}}
    Scores are None for games that haven't started or haven't posted a
    score yet — callers should treat that the same as "not decided"."""
    data = _get(sport_key, "scores", daysFrom=days_from)
    out = {}
    for event in data:
        home_team = event.get("home_team")
        away_team = event.get("away_team")
        home_score = away_score = None
        for s in (event.get("scores") or []):
            if s.get("name") == home_team:
                home_score = s.get("score")
            elif s.get("name") == away_team:
                away_score = s.get("score")
        out[event["id"]] = {
            "completed": bool(event.get("completed")),
            "home_score": int(home_score) if home_score is not None else None,
            "away_score": int(away_score) if away_score is not None else None,
        }
    return out
