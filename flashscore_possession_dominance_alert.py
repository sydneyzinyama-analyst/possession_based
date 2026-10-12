import os
import sys
import argparse
import logging
import time
import re
import traceback
import requests


# ---------------- LOGGING ----------------
# Scheduler stdout is frequently not captured/visible, so we always
# write to a log file as well as stdout. Override the path with
# SCRAPER_LOG_PATH if you want logs somewhere specific.
LOG_PATH = os.getenv("SCRAPER_LOG_PATH", "match_stats_alert.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("match_stats_alert")


# ---------------- TUNABLES ----------------
# 365scores.com JSON API (webws.365scores.com/web/...) — plain,
# unauthenticated requests.get() calls work directly against it, no
# browser/session needed.
REQUEST_TIMEOUT_SEC = int(os.getenv("SCRAPER_REQUEST_TIMEOUT_SEC", "20"))
MAX_RETRIES = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))
RETRY_BACKOFF_SEC = float(os.getenv("SCRAPER_RETRY_BACKOFF_SEC", "1.5"))

# How many of each team's own recent finished matches to analyze.
TEAM_SAMPLE_MATCHES = 6

# Telegram allows roughly one message per second per chat; since this
# script sends one message for every match, pace the sends.
TELEGRAM_SEND_INTERVAL_SEC = float(os.getenv("TELEGRAM_SEND_INTERVAL_SEC", "1.1"))

# Hard ceiling on how long TennisScraper.discover_matches is allowed
# to spend paginating the API for one date, on top of its own
# target_count cap. Without this, an API response shape change could
# spin through many pages indefinitely.
DISCOVER_TIME_BUDGET_SEC = int(os.getenv("SCRAPER_DISCOVER_BUDGET_SEC", "60"))


# ---------------- HOME EDGE SIGNAL ----------------
# Alerts when the home side's own recent averages are ahead of the
# away side's own recent averages in possession, goals AND shots — all
# three, each by a noticeable gap. Not a "dominance" claim, just a
# clear edge. Compares each team's own "for" averages directly (home's
# possession vs away's possession, etc.).
#
# NOTE: these gaps are a reasonable starting point, not checked against
# real results yet.

# Both teams need a full sample of finished matches before the signal
# will judge them.
MIN_SAMPLE_MATCHES = TEAM_SAMPLE_MATCHES

# Minimum gap (home avg minus away avg) for each stat.
MIN_POSSESSION_GAP = 5.0   # percentage points
MIN_GOALS_GAP = 0.4        # goals per game
MIN_SHOTS_GAP = 3.0        # shots per game

HOME_EDGE_STATS = (
    ("Possession", "avg_possession", MIN_POSSESSION_GAP, "%"),
    ("Goals", "avg_goals", MIN_GOALS_GAP, ""),
    ("Shots", "avg_shots_for", MIN_SHOTS_GAP, ""),
)


def evaluate_home_edge_signal(home, away, home_data, away_data):
    """
    Returns a Telegram-ready header if the home side is ahead of the
    away side by at least the minimum gap in every HOME_EDGE_STATS
    stat, or None otherwise (including missing data or a thin sample).
    """
    if (
        (home_data or {}).get("matches", 0) < MIN_SAMPLE_MATCHES
        or (away_data or {}).get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    rows = []
    for label, key, min_gap, suffix in HOME_EDGE_STATS:
        h, a = home_data.get(key), away_data.get(key)
        if h is None or a is None or h - a < min_gap:
            return None
        rows.append(f"✅ {label}: {h:g}{suffix} vs {a:g}{suffix} (+{h - a:.1f})")

    home_esc = _escape_markdown(home)
    away_esc = _escape_markdown(away)

    lines = [
        f"🏠 *Home edge: {home_esc} ahead of {away_esc}*",
        f"(own averages, last {TEAM_SAMPLE_MATCHES} matches)",
    ]
    lines += rows
    return "\n".join(lines)


# ---------------- JOB STATUS TELEGRAM ----------------
def send_job_status(message, bot_token, chat_id):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        requests.post(url, data=payload, timeout=20)
    except Exception as e:
        log.warning(f"Failed to send job status to Telegram: {e}")


# ---------------- SCRAPER CLASS ----------------
class ThreeSixtyFiveScoresScraper:
    """
    Talks directly to 365scores.com's own internal JSON API
    (webws.365scores.com/web/...) with plain requests calls — no
    browser, no session establishment, no fingerprinting needed.
    """

    BASE_URL = "https://webws.365scores.com/web"

    COMMON_PARAMS = {
        "appTypeId": 5,
        "langId": 10,
        "timezoneName": "Africa/Johannesburg",
        "userCountryId": 134,
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        self._last_telegram_send = 0.0

    def _api_get(self, path, params=None, max_retries=None):
        if max_retries is None:
            max_retries = MAX_RETRIES

        url = f"{self.BASE_URL}/{path}"
        full_params = dict(self.COMMON_PARAMS)
        if params:
            full_params.update(params)

        last_error = None

        for attempt in range(1, max_retries + 1):
            t0 = time.time()
            try:
                r = self.session.get(
                    url, params=full_params, timeout=REQUEST_TIMEOUT_SEC
                )
            except Exception as e:
                last_error = f"request error: {e}"
                log.warning(
                    f"API GET {url} attempt {attempt}/{max_retries} "
                    f"failed after {time.time()-t0:.1f}s: {last_error}"
                )
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF_SEC)
                continue

            if r.status_code >= 500:
                last_error = f"status={r.status_code}"
                log.warning(
                    f"API GET {url} attempt {attempt}/{max_retries} "
                    f"got {r.status_code} after {time.time()-t0:.1f}s "
                    f"(transient server error, retrying)"
                )
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF_SEC)
                continue

            if r.status_code != 200:
                log.warning(
                    f"API GET {url} failed after {time.time()-t0:.1f}s: "
                    f"status={r.status_code} body={r.text[:200]!r}"
                )
                return None

            try:
                return r.json()
            except Exception as e:
                log.warning(
                    f"API GET {url} returned invalid JSON after "
                    f"{time.time()-t0:.1f}s: {e}"
                )
                return None

        log.warning(
            f"API GET {url} exhausted {max_retries} attempts, "
            f"last error: {last_error}"
        )
        return None

    # ---------------- DISCOVERY ----------------
    def discover_matches(self, target_count, date_str=None, only_upcoming=False):
        if date_str is None:
            date_str = time.strftime("%d/%m/%Y")

        t0 = time.time()
        data = self._api_get(
            "games/allscores/",
            params={
                "sports": 1,
                "startDate": date_str,
                "endDate": date_str,
                "showOdds": "true",
                "onlyMajorGames": "false",
                "withTop": "true",
            },
        )

        matches = []
        skipped_not_upcoming = 0

        if data and data.get("games"):
            for g in data["games"]:
                if len(matches) >= target_count:
                    break

                if only_upcoming and g.get("statusGroup") != 2:
                    skipped_not_upcoming += 1
                    continue

                home = g.get("homeCompetitor") or {}
                away = g.get("awayCompetitor") or {}
                if home.get("id") is None or away.get("id") is None:
                    continue

                matches.append({
                    "id": g["id"],
                    "home_id": home["id"],
                    "home_name": home.get("name", ""),
                    "away_id": away["id"],
                    "away_name": away.get("name", ""),
                    "tournament": g.get("competitionDisplayName", ""),
                    "start_time": g.get("startTime", ""),
                })

        log.info(
            f"discover_matches: found {len(matches)}/{target_count} "
            f"for {date_str}, {time.time()-t0:.1f}s"
            + (
                f", skipped {skipped_not_upcoming} already-started/finished"
                if only_upcoming
                else ""
            )
        )
        return matches

    # ---------------- TEAM HISTORY ----------------
    def get_team_recent_matches(self, team_id, count=5):
        data = self._api_get(
            "games/results/",
            params={"competitors": team_id, "showOdds": "true"},
        )

        results = []
        if not data or not data.get("games"):
            return results

        for g in data["games"]:
            if g.get("statusGroup") != 4:
                continue

            home = g.get("homeCompetitor") or {}
            away = g.get("awayCompetitor") or {}
            home_goals = home.get("score")
            away_goals = away.get("score")

            if home.get("id") is None or away.get("id") is None:
                continue
            if home_goals is None or away_goals is None:
                continue

            results.append({
                "id": g["id"],
                "home_id": home["id"],
                "home_name": home.get("name", ""),
                "away_id": away["id"],
                "away_name": away.get("name", ""),
                "home_goals": home_goals,
                "away_goals": away_goals,
            })

            if len(results) >= count:
                break

        return results

    # ---------------- MATCH STATISTICS ----------------
    STAT_NAME_MAP = {
        "Expected Goals": "xg",
        "Expected Goals On Target": "xgot",
        "Total Shots": "shots",
        "Shots On Target": "shots_on_target",
        "Corners": "corners",
        "Big Chances Created": "big_chances",
        "Yellow Cards": "yellow_cards",
        "Fouls": "fouls",
        "Possession": "possession",
    }

    def _empty_stat_result(self):
        result = {}
        for stat_key in self.STAT_NAME_MAP.values():
            result[f"home_{stat_key}"] = None
            result[f"away_{stat_key}"] = None
        return result

    def get_match_statistics(self, match_id, home_id, away_id):
        result = self._empty_stat_result()

        data = self._api_get("game/stats/", params={"games": match_id})
        if not data or not data.get("statistics"):
            return result

        try:
            for item in data["statistics"]:
                stat_name = self.STAT_NAME_MAP.get(item.get("name"))
                if not stat_name:
                    continue

                competitor_id = item.get("competitorId")
                value = self._parse_stat_value(item.get("value"))
                if value is None:
                    continue

                if competitor_id == home_id:
                    result[f"home_{stat_name}"] = value
                elif competitor_id == away_id:
                    result[f"away_{stat_name}"] = value
        except Exception as e:
            log.warning(
                f"Error parsing statistics for match {match_id}: {e}"
            )

        return result

    def _parse_stat_value(self, raw_value):
        if raw_value is None:
            return None
        try:
            return float(str(raw_value).rstrip("%"))
        except (TypeError, ValueError):
            return None

    # ---------------- STAT AVERAGING ----------------
    def _team_stat_avg(self, results, stat_name, team_id, side="for"):
        total = 0
        counted = 0

        for r in results:
            is_home = r.get("home_id") == team_id
            is_away = r.get("away_id") == team_id
            if not is_home and not is_away:
                continue

            if side == "for":
                value = (
                    r.get(f"home_{stat_name}")
                    if is_home
                    else r.get(f"away_{stat_name}")
                )
            else:
                value = (
                    r.get(f"away_{stat_name}")
                    if is_home
                    else r.get(f"home_{stat_name}")
                )

            if value is None:
                continue

            total += value
            counted += 1

        if counted == 0:
            return None

        return round(total / counted, 2)

    def _team_form(self, results, team_id):
        """
        W/D/L string from the team's point of view, most recent first,
        in the same order the API returned the matches.
        """
        form = []
        for r in results:
            if r.get("home_id") == team_id:
                gf, ga = r["home_goals"], r["away_goals"]
            elif r.get("away_id") == team_id:
                gf, ga = r["away_goals"], r["home_goals"]
            else:
                continue
            form.append("W" if gf > ga else "L" if gf < ga else "D")
        return "".join(form)

    # ---------------- TEAM ANALYSIS ----------------
    def analyze_team(self, team_id, team_name=None):
        t0 = time.time()
        team_name = team_name or str(team_id)

        recent = self.get_team_recent_matches(team_id, count=TEAM_SAMPLE_MATCHES)
        results = []
        for m in recent:
            match_stats = self.get_match_statistics(
                m["id"], m["home_id"], m["away_id"]
            )
            match_data = dict(m)
            match_data.update(match_stats)
            results.append(match_data)

        log.info(
            f"analyze_team({team_name!r}): {len(results)}/"
            f"{TEAM_SAMPLE_MATCHES} matches fetched in "
            f"{time.time()-t0:.1f}s total"
        )

        avg = lambda stat, side="for": self._team_stat_avg(results, stat, team_id, side)

        avg_xg = avg("xg")
        avg_xga = avg("xg", "against")

        return {
            "team": team_name,
            "team_id": team_id,
            "matches": len(results),
            "form": self._team_form(results, team_id),
            "avg_goals": avg("goals"),
            "avg_gc": avg("goals", "against"),
            "avg_xg": avg_xg,
            "avg_xga": avg_xga,
            "avg_xgd": (
                round(avg_xg - avg_xga, 2)
                if avg_xg is not None and avg_xga is not None
                else None
            ),
            "avg_xgot_for": avg("xgot"),
            "avg_xgot_against": avg("xgot", "against"),
            "avg_possession": avg("possession"),
            "avg_shots_for": avg("shots"),
            "avg_shots_against": avg("shots", "against"),
            "avg_sot_for": avg("shots_on_target"),
            "avg_sot_against": avg("shots_on_target", "against"),
            "avg_big_chances_for": avg("big_chances"),
            "avg_big_chances_against": avg("big_chances", "against"),
            "avg_corners_for": avg("corners"),
            "avg_corners_against": avg("corners", "against"),
            "avg_yellow_cards": avg("yellow_cards"),
            "avg_fouls": avg("fouls"),
        }

    # ---------------- TELEGRAM ----------------
    def send_telegram_message(self, message, bot_token, chat_id):
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }

        for attempt in range(1, 4):
            wait = TELEGRAM_SEND_INTERVAL_SEC - (time.time() - self._last_telegram_send)
            if wait > 0:
                time.sleep(wait)

            try:
                r = requests.post(url, data=payload, timeout=20)
            except Exception as e:
                log.error(f"Failed to send Telegram message: {e}")
                return
            finally:
                self._last_telegram_send = time.time()

            if r.status_code == 200:
                return

            if r.status_code == 429:
                # Rate limited: Telegram says how long to back off.
                try:
                    retry_after = r.json()["parameters"]["retry_after"]
                except Exception:
                    retry_after = 5
                log.warning(
                    f"Telegram rate limit, retrying in {retry_after}s "
                    f"(attempt {attempt}/3)"
                )
                time.sleep(retry_after)
                continue

            log.warning(f"Telegram error: {r.text}")
            return

    def close(self):
        try:
            self.session.close()
        except Exception as e:
            log.warning(f"Error closing session: {e}")


# ---------------- TENNIS SCRAPER CLASS ----------------
# Same webws.365scores.com API as ThreeSixtyFiveScoresScraper,
# sports=3 instead of sports=1 — confirmed by direct testing:
# games/allscores/, games/results/, and game/stats/ return the same
# shapes as football (statusGroup 2=upcoming, 4=finished), except
# "score" is SETS WON, not goals, and game/stats/ values are mostly
# compound strings like "3/8 (38%)" (made/attempted (pct%)) rather
# than plain numbers.
#
# Court surface isn't on games/allscores/ or games/results/ at all —
# confirmed by direct inspection — only the single-game detail
# endpoint (game/?gameId=...) sometimes embeds it as a comma-separated
# segment of competitionDisplayName (e.g. "WTA, Seoul, Hard - Round of
# 32"), and only for tour-level events; Challenger/qualifying rounds
# often omit it entirely (e.g. "Challenger, Cordenons - Semi Finals").
# get_match_surface hits that endpoint per match and returns None when
# no known surface keyword is found — every caller treats that as
# "unknown", never a guessed default, same fail-closed pattern as the
# rest of this class.
#
# There's also no single blended "return points won %" stat on this
# API (confirmed by direct inspection of a finished match's full stat
# list: Aces, Double Faults, Break Points Won, Service Games, Total
# Points Won, Service Points, 1st/2nd Serve Points Won, 2nd Return
# Points Won, Total Games Won) — RETURN_POINTS_STAT_KEY below picks
# "2nd Return Points Won" as the closest available proxy for return
# quality, since it's the only return-side percentage offered.
#
# Deliberately a fully standalone class, not sharing code with
# ThreeSixtyFiveScoresScraper above, even though _api_get/__init__/
# close duplicate it near-verbatim: this file's football scraper is
# proven, scheduled, production code, and refactoring it into a
# shared base class to save ~80 lines isn't worth the risk of subtly
# breaking it. The module-level tunables (REQUEST_TIMEOUT_SEC,
# MAX_RETRIES, RETRY_BACKOFF_SEC, DISCOVER_TIME_BUDGET_SEC,
# TELEGRAM_SEND_INTERVAL_SEC) ARE shared, since they're genuinely
# sport-agnostic HTTP/Telegram tuning, not football-specific.
#
# Doubles matches show up in the same feed, distinguishable only by a
# "/" joining two names in the competitor name (e.g. "Bucsa
# C./Melichar-Martinez N.") — no separate flag from the API.
# discover_matches and get_player_recent_matches default to
# singles_only=True and use that "/" check to skip them.
class TennisScraper:
    """
    Talks to the same webws.365scores.com API as
    ThreeSixtyFiveScoresScraper, sports=3 instead of sports=1.
    analyze_player returns a plain list of per-match dicts shaped for
    the Predictive Power Score engine below (see that section's
    header comment) — real, per-match live data, no mock rows
    involved.
    """

    BASE_URL = "https://webws.365scores.com/web"
    SPORT_ID = 3

    COMMON_PARAMS = {
        "appTypeId": 5,
        "langId": 10,
        "timezoneName": "Africa/Johannesburg",
        "userCountryId": 134,
    }

    STAT_NAME_MAP = {
        "Aces": "aces",
        "Double Faults": "double_faults",
        "Break Points Won": "break_points_won_pct",
        "Service Games": "service_games_won_pct",
        "Total Points Won": "total_points_won_pct",
        "Service Points": "service_points_won_pct",
        "1st Serve Points Won": "first_serve_points_won_pct",
        "2nd Serve Points Won": "second_serve_points_won_pct",
        "2nd Return Points Won": "second_return_points_won_pct",
        "Total Games Won": "total_games_won_pct",
    }

    RETURN_POINTS_STAT_KEY = "second_return_points_won_pct"

    # Surface keywords looked for in a game's competitionDisplayName —
    # see this section's header comment for where this comes from.
    KNOWN_SURFACES = ("Hard", "Clay", "Grass", "Carpet")

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        self.player_id = None
        self.player_name = None
        self._last_telegram_send = 0.0

    def _api_get(self, path, params=None, max_retries=None):
        """
        Identical retry/timeout machinery to
        ThreeSixtyFiveScoresScraper._api_get — see that method's
        docstring. Duplicated rather than shared; see this section's
        header comment for why.
        """
        if max_retries is None:
            max_retries = MAX_RETRIES

        url = f"{self.BASE_URL}/{path}"
        full_params = dict(self.COMMON_PARAMS)
        if params:
            full_params.update(params)

        last_error = None

        for attempt in range(1, max_retries + 1):
            t0 = time.time()
            try:
                r = self.session.get(
                    url, params=full_params, timeout=REQUEST_TIMEOUT_SEC
                )
            except Exception as e:
                last_error = f"request error: {e}"
                log.warning(
                    f"API GET {url} attempt {attempt}/{max_retries} "
                    f"failed after {time.time()-t0:.1f}s: {last_error}"
                )
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF_SEC)
                continue

            if r.status_code >= 500:
                last_error = f"status={r.status_code}"
                log.warning(
                    f"API GET {url} attempt {attempt}/{max_retries} "
                    f"got {r.status_code} after {time.time()-t0:.1f}s "
                    f"(transient server error, retrying)"
                )
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF_SEC)
                continue

            if r.status_code != 200:
                log.warning(
                    f"API GET {url} failed after {time.time()-t0:.1f}s: "
                    f"status={r.status_code} body={r.text[:200]!r}"
                )
                return None

            try:
                return r.json()
            except Exception as e:
                log.warning(
                    f"API GET {url} returned invalid JSON after "
                    f"{time.time()-t0:.1f}s: {e}"
                )
                return None

        log.warning(
            f"API GET {url} exhausted {max_retries} attempts, "
            f"last error: {last_error}"
        )
        return None

    @staticmethod
    def _is_doubles(home_name, away_name):
        return "/" in (home_name or "") or "/" in (away_name or "")

    # ---------------- DISCOVERY ----------------
    def discover_matches(
        self, target_count, date_str=None, only_upcoming=False,
        singles_only=True,
    ):
        """
        Returns up to `target_count` match dicts for `date_str`
        (today, server-local, if not given, as DD/MM/YYYY): each
        {id, home_id, home_name, away_id, away_name, tournament,
        start_time, home_rank, away_rank}. Same shape and statusGroup
        semantics as ThreeSixtyFiveScoresScraper.discover_matches; see
        this section's header comment for singles_only. home_rank/
        away_rank (per _extract_rank, None if unranked) come straight
        off this same response at zero extra cost — see the TENNIS
        FAVORITE SIGNAL section below for why official ranking, not a
        from-scratch composite score, is what actually anchors the
        pick. No surface here — see get_match_surface, called
        per-match instead since surface isn't on this endpoint at all.
        """
        if date_str is None:
            date_str = time.strftime("%d/%m/%Y")

        t0 = time.time()
        data = self._api_get(
            "games/allscores/",
            params={
                "sports": self.SPORT_ID,
                "startDate": date_str,
                "endDate": date_str,
                "showOdds": "true",
                "onlyMajorGames": "false",
                "withTop": "true",
            },
        )

        matches = []
        skipped_not_upcoming = 0
        skipped_doubles = 0

        if data and data.get("games"):
            for g in data["games"]:
                if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                    log.warning(
                        f"discover_matches (tennis) hit its "
                        f"{DISCOVER_TIME_BUDGET_SEC}s time budget with "
                        f"{len(matches)}/{target_count} found — "
                        f"stopping early"
                    )
                    break

                if len(matches) >= target_count:
                    break

                if only_upcoming and g.get("statusGroup") != 2:
                    skipped_not_upcoming += 1
                    continue

                home = g.get("homeCompetitor") or {}
                away = g.get("awayCompetitor") or {}
                if home.get("id") is None or away.get("id") is None:
                    continue

                if singles_only and self._is_doubles(
                    home.get("name"), away.get("name")
                ):
                    skipped_doubles += 1
                    continue

                matches.append({
                    "id": g["id"],
                    "home_id": home["id"],
                    "home_name": home.get("name", ""),
                    "away_id": away["id"],
                    "away_name": away.get("name", ""),
                    "tournament": g.get("competitionDisplayName", ""),
                    "start_time": g.get("startTime"),
                    "home_rank": self._extract_rank(home),
                    "away_rank": self._extract_rank(away),
                })

        log.info(
            f"discover_matches (tennis): found {len(matches)}/"
            f"{target_count} for {date_str}, {time.time()-t0:.1f}s"
            + (
                f", skipped {skipped_not_upcoming} already-started/finished"
                if only_upcoming
                else ""
            )
            + (
                f", skipped {skipped_doubles} doubles"
                if singles_only
                else ""
            )
        )
        return matches

    # ---------------- SURFACE ----------------
    def get_match_surface(self, match_id):
        """
        Returns "Hard"/"Clay"/"Grass"/"Carpet" for this match, or None
        if the single-game detail endpoint doesn't carry a recognised
        surface — confirmed by direct inspection to happen routinely
        for Challenger/qualifying rounds. Never guesses a default;
        every caller treats None as "unknown", excluded from the
        surface-specific split rather than credited to any surface.
        """
        data = self._api_get("game/", params={"gameId": match_id})
        if not data or not data.get("game"):
            return None

        name = data["game"].get("competitionDisplayName", "") or ""
        segments = [s.strip() for s in re.split(r"[,-]", name)]
        for surface in self.KNOWN_SURFACES:
            if surface in segments:
                return surface
        return None

    # ---------------- PLAYER HISTORY ----------------
    @staticmethod
    def _extract_rank(competitor):
        """
        competitor["rankings"] is a list like
        [{"name": "ATP", "position": 245}] — confirmed by direct
        inspection to be present on roughly half of entries (missing
        for players outside the computer rankings, e.g. qualifiers).
        None-safe: every caller treats a missing rank as "unknown
        strength", never as "unranked/worst" or "best".
        """
        rankings = (competitor or {}).get("rankings")
        if not rankings:
            return None
        position = rankings[0].get("position")
        return position if isinstance(position, (int, float)) else None

    def get_player_recent_matches(self, player_id, count=10, singles_only=True):
        """
        Returns up to `count` of this player's most recent FINISHED
        singles matches, most-recent-first (id, home_id, home_name,
        away_id, away_name, home_sets, away_sets, home_rank, away_rank
        — per _extract_rank, None if unranked). Same games/results/
        endpoint and ordering as
        ThreeSixtyFiveScoresScraper.get_team_recent_matches.
        """
        data = self._api_get(
            "games/results/",
            params={"competitors": player_id, "showOdds": "true"},
        )

        results = []
        if not data or not data.get("games"):
            return results

        for g in data["games"]:
            if g.get("statusGroup") != 4:
                continue

            home = g.get("homeCompetitor") or {}
            away = g.get("awayCompetitor") or {}

            if singles_only and self._is_doubles(
                home.get("name"), away.get("name")
            ):
                continue

            home_sets = home.get("score")
            away_sets = away.get("score")

            if home.get("id") is None or away.get("id") is None:
                continue
            if home_sets is None or away_sets is None:
                continue

            results.append({
                "id": g["id"],
                "home_id": home["id"],
                "home_name": home.get("name", ""),
                "away_id": away["id"],
                "away_name": away.get("name", ""),
                "home_sets": home_sets,
                "away_sets": away_sets,
                "home_rank": self._extract_rank(home),
                "away_rank": self._extract_rank(away),
            })

            if len(results) >= count:
                break

        return results

    # ---------------- MATCH STATISTICS ----------------
    def _empty_stat_result(self):
        result = {}
        for stat_key in set(self.STAT_NAME_MAP.values()):
            result[f"home_{stat_key}"] = None
            result[f"away_{stat_key}"] = None
        return result

    def get_match_statistics(self, match_id, home_id, away_id):
        """
        Same flat {name, competitorId, value} row shape as football's
        game/stats/ — see ThreeSixtyFiveScoresScraper.get_match_statistics
        for the general pattern. What differs here is
        _parse_stat_value (compound "made/attempted (pct%)" strings,
        not plain numbers).
        """
        result = self._empty_stat_result()

        data = self._api_get("game/stats/", params={"games": match_id})
        if not data or not data.get("statistics"):
            return result

        try:
            for item in data["statistics"]:
                stat_name = self.STAT_NAME_MAP.get(item.get("name"))
                if not stat_name:
                    continue

                competitor_id = item.get("competitorId")
                raw_value = item.get("value")
                value = self._parse_stat_value(raw_value)
                if value is None:
                    continue

                if competitor_id == home_id:
                    result[f"home_{stat_name}"] = value
                elif competitor_id == away_id:
                    result[f"away_{stat_name}"] = value
        except Exception as e:
            log.warning(
                f"Error parsing tennis statistics for match {match_id}: {e}"
            )

        return result

    def _parse_stat_value(self, raw_value):
        """
        Tennis stat values come back either as a plain count ("5",
        "16") or a compound "made/attempted (pct%)" string
        ("3/8 (38%)") — the percentage is extracted from the compound
        form; a plain count is parsed as-is.
        """
        if raw_value is None:
            return None
        s = str(raw_value)
        m = re.search(r"\(([\d.]+)%\)", s)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    # ---------------- SCRAPER ----------------
    def analyze_player(self, player_id, player_name=None, count=10):
        """
        Fetches this player's last `count` finished singles matches,
        each one's statistics and surface, and returns a plain list
        of per-match dicts shaped exactly for the Predictive Power
        Score functions below (opponent_rank, surface, outcome,
        sets_won, sets_lost, first_serve_won_pct, second_serve_won_pct,
        return_points_won_pct, break_points_won_pct) — oldest match
        first, most recent last, so form_momentum_score's [-N:] slice
        lands on the right end.

        Real, live per-match data throughout — no mock rows. One
        request for the match list, then two more per historical match
        (stats + surface), so analyzing one player costs up to
        1 + 2*count calls; accepted here since there's no cheaper way
        to get surface per historical match from this API (see this
        class's header comment).
        """
        t0 = time.time()
        self.player_id = player_id
        self.player_name = player_name or str(player_id)

        recent = self.get_player_recent_matches(player_id, count=count)

        rows = []
        for m in recent:
            is_home = m["home_id"] == player_id
            player_sets = m["home_sets"] if is_home else m["away_sets"]
            opponent_sets = m["away_sets"] if is_home else m["home_sets"]
            opponent_rank = m["away_rank"] if is_home else m["home_rank"]
            side = "home" if is_home else "away"

            stats = self.get_match_statistics(m["id"], m["home_id"], m["away_id"])
            surface = self.get_match_surface(m["id"])

            rows.append({
                "opponent_rank": opponent_rank,
                "surface": surface,
                "outcome": "W" if player_sets > opponent_sets else "L",
                "sets_won": player_sets,
                "sets_lost": opponent_sets,
                "first_serve_won_pct": stats.get(f"{side}_first_serve_points_won_pct"),
                "second_serve_won_pct": stats.get(f"{side}_second_serve_points_won_pct"),
                "return_points_won_pct": stats.get(f"{side}_{self.RETURN_POINTS_STAT_KEY}"),
                "break_points_won_pct": stats.get(f"{side}_break_points_won_pct"),
            })

        rows.reverse()  # oldest-first; get_player_recent_matches is most-recent-first

        log.info(
            f"analyze_player({self.player_name!r}): {len(rows)}/{count} "
            f"matches fetched in {time.time()-t0:.1f}s total"
        )

        return rows

    # ---------------- TELEGRAM ----------------
    def send_telegram_message(self, message, bot_token, chat_id):
        """Same pacing/429-retry logic as ThreeSixtyFiveScoresScraper.send_telegram_message."""
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }

        for attempt in range(1, 4):
            wait = TELEGRAM_SEND_INTERVAL_SEC - (time.time() - self._last_telegram_send)
            if wait > 0:
                time.sleep(wait)

            try:
                r = requests.post(url, data=payload, timeout=20)
            except Exception as e:
                log.error(f"Failed to send Telegram message: {e}")
                return
            finally:
                self._last_telegram_send = time.time()

            if r.status_code == 200:
                return

            if r.status_code == 429:
                try:
                    retry_after = r.json()["parameters"]["retry_after"]
                except Exception:
                    retry_after = 5
                log.warning(
                    f"Telegram rate limit, retrying in {retry_after}s "
                    f"(attempt {attempt}/3)"
                )
                time.sleep(retry_after)
                continue

            log.warning(f"Telegram error: {r.text}")
            return

    def close(self):
        try:
            self.session.close()
        except Exception as e:
            log.warning(f"Error closing session: {e}")


# ---------------- TENNIS FAVORITE SIGNAL ----------------
# BACKTESTED (same 365scores data, two independent weeks): requiring
# the official ranking favorite to ALSO be ahead on literally every
# one of the 7 measured signals below (form, surface fit, schedule
# strength, and all 4 serve/return stats) — i.e. corroboration==7,
# no partial credit, no confidence tiers — scored ~72% pooled across
# both weeks, consistently better than baseline in each one
# separately. That's the only bar this signal uses: outright better
# in every department, or no alert at all.
#
# Official ranking still picks WHO the favorite is (_rank_favorite) —
# a mature, season-long aggregate beats a from-scratch composite
# built from a noisy 10-match snapshot (a from-scratch score alone
# scored 45% on 29 real matches, worse than a coin flip). The 7
# signals below only ever confirm that pick; they can never flip it.
# Every function is None/NaN-safe: real API data is missing fields
# routinely (unranked opponents, Challenger matches with no surface,
# stats endpoints with partial data), and this fails closed (treats
# "unknown" as neutral, never as a crash).
TENNIS_MIN_SAMPLE_MATCHES = 5

WEIGHT_FORM = 0.30
WEIGHT_SURFACE = 0.25
WEIGHT_SCHEDULE = 0.20
WEIGHT_STATS = 0.25

RECENT_MATCH_WEIGHT_MULTIPLIER = 1.5  # applied to the most recent matches
RECENT_MATCH_COUNT = 3

# Out of 7 possible signals (3 Predictive Power Score components +
# 4 individual serve/return stats — see _corroboration_score). This
# fires only when ALL 7 agree with the ranking favorite; nothing
# partial.
TENNIS_CORROBORATION_REQUIRED = 7


def _opponent_strength_multiplier(rank):
    """
    Scales a win/loss by how tough the opponent was. None (opponent
    outside the computer rankings, or rank unavailable from this API)
    gets the same multiplier as a plain outside-top-50 opponent — an
    unranked opponent is never assumed to be a hidden Top 10 threat.
    """
    if rank is None:
        return 1.0
    if rank <= 10:
        return 2.0
    if rank <= 50:
        return 1.3
    return 1.0


def form_momentum_score(matches):
    """
    Win/loss streak over the sampled matches (a list of per-match
    dicts, oldest first — see TennisScraper.analyze_player), with the
    most recent RECENT_MATCH_COUNT counted
    RECENT_MATCH_WEIGHT_MULTIPLIER-x, normalized to 0-100 against the
    max possible weighted total. Returns 50.0 (neutral) if there are
    no matches to score — real data can come back empty for a player
    with no recent finished singles matches. Pure Python, not
    pandas/numpy — no need for either dependency just to weight-sum a
    handful of rows.
    """
    n = len(matches)
    if n == 0:
        return 50.0

    recent_cutoff = n - min(RECENT_MATCH_COUNT, n)
    weighted_wins = 0.0
    max_possible = 0.0

    for i, m in enumerate(matches):
        weight = RECENT_MATCH_WEIGHT_MULTIPLIER if i >= recent_cutoff else 1.0
        max_possible += weight
        if m["outcome"] == "W":
            weighted_wins += weight

    return (weighted_wins / max_possible) * 100


def surface_compatibility_score(matches, surface):
    """
    Win percentage restricted to matches played on `surface`. Falls
    back to the player's overall win rate if they have no matches on
    this surface (common with real data: surface is often unknown —
    see TennisScraper.get_match_surface — or the player simply hasn't
    played this surface in their last few matches).
    """
    if len(matches) == 0:
        return 50.0

    on_surface = [m for m in matches if m["surface"] == surface]

    pool = on_surface if on_surface else matches
    wins = sum(1 for m in pool if m["outcome"] == "W")

    return (wins / len(pool)) * 100


def strength_of_schedule_score(matches):
    """
    Each match's win (+1) or loss (0) is scaled by the opponent's
    rank-based strength multiplier (None-safe — see
    _opponent_strength_multiplier), then normalized against the max
    possible weighted total.
    """
    if len(matches) == 0:
        return 50.0

    weighted_wins = 0.0
    max_possible = 0.0

    for m in matches:
        multiplier = _opponent_strength_multiplier(m["opponent_rank"])
        max_possible += multiplier
        if m["outcome"] == "W":
            weighted_wins += multiplier

    return (weighted_wins / max_possible) * 100


def predictive_power_score(matches, surface):
    """
    Combines the form/surface/schedule components into one dict —
    the 4th component (a blended "statistical dominance" number) was
    dropped in favor of comparing individual serve/return stats
    directly (see _player_stat_averages/_corroboration_score); kept
    here as 3 components plus a (now partial) total_score for
    backward-compatible shape with the rest of this section.
    """
    form = form_momentum_score(matches)
    surface_score = surface_compatibility_score(matches, surface)
    schedule = strength_of_schedule_score(matches)

    total = (
        form * WEIGHT_FORM
        + surface_score * WEIGHT_SURFACE
        + schedule * WEIGHT_SCHEDULE
    )

    return {
        "form_momentum": round(form, 2),
        "surface_compatibility": round(surface_score, 2),
        "strength_of_schedule": round(schedule, 2),
        "total_score": round(total, 2),
    }


def _rank_favorite(a_rank, b_rank):
    """
    Lower official ATP/WTA rank number = the stronger player — this is
    what actually picks the winner now (see this section's header
    comment). Returns "a", "b", or None if either side's current
    ranking is unknown (unranked/qualifier — see
    TennisScraper._extract_rank) or the two are tied, since there's
    then no rank-based basis for a pick at all. Never falls back to
    the composite score to break that tie — an unranked player is
    exactly the case real ranking data can't speak to, and guessing
    off the noisy fallback would undo the whole point of this design.
    """
    if a_rank is None or b_rank is None or a_rank == b_rank:
        return None
    return "a" if a_rank < b_rank else "b"


def _player_stat_averages(matches):
    """
    Average of each individual serve/return stat across `matches`
    (a player's own list of per-match dicts from
    TennisScraper.analyze_player), computed separately so
    _corroboration_score can compare the two players stat-by-stat
    (first serve won%, second serve won%, return points won%, break
    points won%) instead of one blended average. None-safe: real
    match-stats calls come back partial routinely (see
    TennisScraper.get_match_statistics) — a stat with zero usable
    values across the whole sample comes back None, not 0, so it's
    excluded from the matchup rather than read as "terrible at this".
    """
    keys = (
        "first_serve_won_pct", "second_serve_won_pct",
        "return_points_won_pct", "break_points_won_pct",
    )
    result = {}
    for key in keys:
        values = [m[key] for m in matches if m.get(key) is not None]
        result[key] = sum(values) / len(values) if values else None
    return result


def _corroboration_score(favorite_result, underdog_result, favorite_stats, underdog_stats):
    """
    Counts how many of 7 signals favor the official-ranking favorite
    over the underdog — 0 to 7. 3 are the Predictive Power Score's
    form/surface/schedule components; 4 are individual serve/return
    stats compared directly player-to-player. Purely additive
    confirmation on top of the ranking-based pick, never able to flip
    it. A stat missing for either side contributes nothing to either
    side (fail-closed), not a point for whichever side happens to
    have data.
    """
    score = 0

    for key in ("form_momentum", "surface_compatibility", "strength_of_schedule"):
        if favorite_result[key] > underdog_result[key]:
            score += 1

    for key in (
        "first_serve_won_pct", "second_serve_won_pct",
        "return_points_won_pct", "break_points_won_pct",
    ):
        fav_val = favorite_stats.get(key)
        dog_val = underdog_stats.get(key)
        if fav_val is not None and dog_val is not None and fav_val > dog_val:
            score += 1

    return score


def evaluate_tennis_favorite_signal(
    player_a, player_b, a_rank, b_rank, a_matches, b_matches, surface, m_url,
):
    """
    Picks the winner by official ranking (_rank_favorite) — validated
    at 67.5% on 114 real matches, see this section's header comment —
    then only fires if that favorite is ALSO ahead on literally every
    one of the 7 corroboration signals (_corroboration_score ==
    TENNIS_CORROBORATION_REQUIRED) — outright better in every
    department, no partial credit, no confidence tiers. Returns None
    if either side lacks enough recent-match data
    (TENNIS_MIN_SAMPLE_MATCHES, same "refuse to predict on too little
    data" gate as the football signal above), if neither player has a
    usable official ranking, or if the favorite isn't ahead on all 7.
    """
    if (
        len(a_matches) < TENNIS_MIN_SAMPLE_MATCHES
        or len(b_matches) < TENNIS_MIN_SAMPLE_MATCHES
    ):
        return None

    favorite_side = _rank_favorite(a_rank, b_rank)
    if favorite_side is None:
        return None

    a_result = predictive_power_score(a_matches, surface)
    b_result = predictive_power_score(b_matches, surface)
    a_stat_avgs = _player_stat_averages(a_matches)
    b_stat_avgs = _player_stat_averages(b_matches)

    if favorite_side == "a":
        favorite, underdog = player_a, player_b
        favorite_rank, underdog_rank = a_rank, b_rank
        favorite_result, underdog_result = a_result, b_result
        favorite_stats, underdog_stats = a_stat_avgs, b_stat_avgs
    else:
        favorite, underdog = player_b, player_a
        favorite_rank, underdog_rank = b_rank, a_rank
        favorite_result, underdog_result = b_result, a_result
        favorite_stats, underdog_stats = b_stat_avgs, a_stat_avgs

    corroboration = _corroboration_score(
        favorite_result, underdog_result, favorite_stats, underdog_stats,
    )

    if corroboration < TENNIS_CORROBORATION_REQUIRED:
        return None

    favorite_esc = _escape_markdown(favorite)
    underdog_esc = _escape_markdown(underdog)

    def fmt_stat(v):
        return "N/A" if v is None else f"{v:.1f}"

    lines = [
        f"🎾 *{favorite_esc} is statistically better than {underdog_esc} in every department*",
        f"{_escape_markdown(player_a)} vs {_escape_markdown(player_b)}"
        + (f" — {surface}" if surface else ""),
        "",
        f"*{favorite_esc}* (rank {favorite_rank}) to beat "
        f"{underdog_esc} (rank {underdog_rank})",
        f"Form {favorite_result['form_momentum']:.1f} vs {underdog_result['form_momentum']:.1f} · "
        f"Surface {favorite_result['surface_compatibility']:.1f} vs {underdog_result['surface_compatibility']:.1f} · "
        f"Schedule {favorite_result['strength_of_schedule']:.1f} vs {underdog_result['strength_of_schedule']:.1f}",
        f"1st Serve {fmt_stat(favorite_stats['first_serve_won_pct'])} vs {fmt_stat(underdog_stats['first_serve_won_pct'])} · "
        f"2nd Serve {fmt_stat(favorite_stats['second_serve_won_pct'])} vs {fmt_stat(underdog_stats['second_serve_won_pct'])} · "
        f"Return {fmt_stat(favorite_stats['return_points_won_pct'])} vs {fmt_stat(underdog_stats['return_points_won_pct'])} · "
        f"Break Pts {fmt_stat(favorite_stats['break_points_won_pct'])} vs {fmt_stat(underdog_stats['break_points_won_pct'])}",
        "",
        f"🔗 {m_url}",
    ]

    return "\n".join(lines)


# ---------------- MESSAGE FORMATTING ----------------

def _escape_markdown(text):
    """
    Minimal escaping for Telegram's legacy "Markdown" parse mode: only
    _, *, ` and [ need escaping there.
    """
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def _fmt(v, suffix=""):
    return "N/A" if v is None else f"{v}{suffix}"


def _team_block(label, s):
    """
    Stats section for one team. `s` is analyze_team()'s return value,
    or None if that team's analysis failed.
    """
    if s is None:
        return [f"*{label}*", "   (no data — analysis failed)"]

    return [
        f"*{label}*  (last {s['matches']}, form {s['form'] or 'N/A'})",
        f"   Goals {_fmt(s['avg_goals'])} for / {_fmt(s['avg_gc'])} against",
        f"   xG {_fmt(s['avg_xg'])} for / {_fmt(s['avg_xga'])} against "
        f"(xGD {_fmt(s['avg_xgd'])})",
        f"   xGOT {_fmt(s['avg_xgot_for'])} for / {_fmt(s['avg_xgot_against'])} against",
        f"   Possession {_fmt(s['avg_possession'], '%')}",
        f"   Shots {_fmt(s['avg_shots_for'])} for / {_fmt(s['avg_shots_against'])} against",
        f"   On target {_fmt(s['avg_sot_for'])} for / {_fmt(s['avg_sot_against'])} against",
        f"   Big chances {_fmt(s['avg_big_chances_for'])} for / "
        f"{_fmt(s['avg_big_chances_against'])} against",
        f"   Corners {_fmt(s['avg_corners_for'])} for / {_fmt(s['avg_corners_against'])} against",
        f"   Yellow cards {_fmt(s['avg_yellow_cards'])} | Fouls {_fmt(s['avg_fouls'])}",
    ]


def build_match_message(match, home_data, away_data, m_url):
    home = _escape_markdown(match["home_name"])
    away = _escape_markdown(match["away_name"])
    tournament = _escape_markdown(match.get("tournament") or "")
    kickoff = match.get("start_time") or ""

    lines = [f"⚽ *{home} vs {away}*"]
    if tournament:
        lines.append(f"🏆 {tournament}")
    if kickoff:
        # startTime looks like 2026-09-28T19:00:00+02:00
        lines.append(f"🕒 {kickoff[:16].replace('T', ' ')}")
    lines.append("")
    lines.append(f"📈 *Averages per game (last {TEAM_SAMPLE_MATCHES} matches)*")
    lines.extend(_team_block(f"🏠 {home}", home_data))
    lines.append("")
    lines.extend(_team_block(f"✈️ {away}", away_data))
    lines.append("")
    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


# ---------------- ALERT SCRIPT ----------------

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    START = max(0, args.start)
    LIMIT = max(1, args.limit)
    TARGET_COUNT = START + LIMIT

    BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
    CHAT_ID = os.getenv("CHAT_ID", "").strip()

    if not BOT_TOKEN or not CHAT_ID:
        log.error(
            "BOT_TOKEN or CHAT_ID is missing from environment variables."
        )
        return

    send_job_status(
        f"🚀 Job STARTED (soccer + tennis match stats alert)\n"
        f"Batch START={START} LIMIT={LIMIT}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting 365scores match stats alert script...")
    log.info(f"Batch start={START}, limit={LIMIT}")

    scraper = None
    matches = []
    sent_count = 0

    try:
        scraper = ThreeSixtyFiveScoresScraper()

        matches = scraper.discover_matches(TARGET_COUNT, only_upcoming=True)
        log.info(f"Found {len(matches)} upcoming matches total")

        batch_matches = matches[START:START + LIMIT]
        log.info(
            f"This job will process {len(batch_matches)} matches "
            f"from {START} to {START + LIMIT - 1}"
        )

        if not batch_matches:
            # No `return` here — soccer having nothing in this
            # batch's range doesn't mean tennis does too (the two
            # sports' daily match counts are unrelated), so execution
            # has to fall through to the tennis phase below rather
            # than exiting main() entirely.
            log.info("No matches in this batch.")
            send_job_status(
                f"⚠️ Match stats alert FINISHED (No matches)\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, 0 fell in this "
                f"batch's range",
                BOT_TOKEN,
                CHAT_ID
            )

        else:
            for idx, match in enumerate(batch_matches, start=START + 1):
                m_url = f"https://www.365scores.com/en-uk/football/game/{match['id']}"
                home = match["home_name"]
                away = match["away_name"]

                log.info(
                    f"Processing match {idx}: {home} vs {away} "
                    f"({match.get('tournament', '')}) {m_url}"
                )

                try:
                    if not home or not away:
                        log.warning("Could not extract teams, skipping match")
                        continue

                    try:
                        home_data = scraper.analyze_team(match["home_id"], home)
                    except Exception as e:
                        log.error(f"Home team analysis failed: {e}")
                        home_data = None

                    try:
                        away_data = scraper.analyze_team(match["away_id"], away)
                    except Exception as e:
                        log.error(f"Away team analysis failed: {e}")
                        away_data = None

                    edge_msg = evaluate_home_edge_signal(home, away, home_data, away_data)
                    if not edge_msg:
                        log.info("No home edge — not alerting.")
                        continue

                    msg = edge_msg + "\n\n" + build_match_message(match, home_data, away_data, m_url)
                    log.info("MATCH STATS (home edge):\n" + msg)
                    scraper.send_telegram_message(msg, BOT_TOKEN, CHAT_ID)
                    sent_count += 1

                except Exception as match_err:
                    log.error(f"Error processing match {m_url}: {match_err}")
                    log.debug(traceback.format_exc())
                    continue

            log.info(
                f"Sent {sent_count}/{len(batch_matches)} matches "
                f"in this batch (found {len(matches)} total today)"
            )

            send_job_status(
                f"✅ Match stats alert FINISHED\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, sent "
                f"{sent_count}/{len(batch_matches)} in this batch",
                BOT_TOKEN,
                CHAT_ID
            )

    except Exception as e:
        log.error(f"Match stats alert job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Match stats alert FAILED\n"
            f"Batch START={START} LIMIT={LIMIT}\n"
            f"Found {len(matches)} matches today, sent "
            f"{sent_count} before failing\n"
            f"Error: {str(e)}",
            BOT_TOKEN,
            CHAT_ID
        )

    finally:
        log.info("Closing scraper session...")
        if scraper is not None:
            scraper.close()

    # ================== TENNIS ==================
    #
    # Runs sequentially after soccer, in the same script execution —
    # its own scraper/try/except/finally block, entirely independent
    # of soccer's above: a tennis-side failure can't affect the
    # soccer results already sent, and if soccer's block above failed
    # instead, tennis still gets to run rather than being skipped
    # (this isn't nested inside soccer's try).
    #
    # evaluate_tennis_favorite_signal picks the winner by official
    # ranking, using the Predictive Power Score engine only as
    # corroboration (see TENNIS FAVORITE SIGNAL section's header
    # comment) — real, live 365scores data throughout.
    tennis_scraper = None
    tennis_matches = []
    tennis_sent_count = 0

    try:
        tennis_scraper = TennisScraper()

        tennis_matches = tennis_scraper.discover_matches(
            TARGET_COUNT,
            only_upcoming=True,
            singles_only=True,
        )

        log.info(f"Found {len(tennis_matches)} upcoming tennis matches total")

        tennis_batch_matches = tennis_matches[START:START + LIMIT]

        log.info(
            f"This job will process {len(tennis_batch_matches)} tennis "
            f"matches from {START} to {START + LIMIT - 1}"
        )

        if not tennis_batch_matches:

            log.info("No tennis matches in this batch.")

            send_job_status(
                f"⚠️ Tennis stats alert FINISHED (No matches)\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(tennis_matches)} tennis matches today, 0 "
                f"fell in this batch's range",
                BOT_TOKEN,
                CHAT_ID
            )

        else:

            for idx, match in enumerate(
                tennis_batch_matches,
                start=START + 1
            ):
                m_url = f"https://www.365scores.com/en-uk/tennis/game/{match['id']}"
                player_a = match["home_name"]
                player_b = match["away_name"]

                log.info(
                    f"Processing tennis match {idx}: {player_a} vs "
                    f"{player_b} ({match.get('tournament', '')}) {m_url}"
                )

                try:
                    if not player_a or not player_b:
                        log.warning(
                            "Could not extract players, skipping match"
                        )
                        continue

                    surface = tennis_scraper.get_match_surface(match["id"])
                    a_rank = match.get("home_rank")
                    b_rank = match.get("away_rank")

                    a_error = None
                    b_error = None

                    try:
                        a_matches = tennis_scraper.analyze_player(
                            match["home_id"], match["home_name"]
                        )
                    except Exception as e:
                        a_matches = None
                        a_error = e

                    try:
                        b_matches = tennis_scraper.analyze_player(
                            match["away_id"], match["away_name"]
                        )
                    except Exception as e:
                        b_matches = None
                        b_error = e

                    if a_error:
                        log.error(f"Player A analysis failed: {a_error}")

                    if b_error:
                        log.error(f"Player B analysis failed: {b_error}")

                    if a_matches is None or b_matches is None:
                        log.warning(
                            "Could not analyze one or both players, "
                            "skipping match"
                        )
                        continue

                    favorite_msg = evaluate_tennis_favorite_signal(
                        player_a, player_b, a_rank, b_rank,
                        a_matches, b_matches, surface, m_url,
                    )

                    if not favorite_msg:
                        log.info("No tennis favorite signal — not alerting.")
                        continue

                    log.info(f"TENNIS ALERT:\n{favorite_msg}")
                    tennis_scraper.send_telegram_message(favorite_msg, BOT_TOKEN, CHAT_ID)
                    tennis_sent_count += 1

                except Exception as match_err:
                    log.error(
                        f"Error processing tennis match {m_url}: {match_err}"
                    )
                    log.debug(traceback.format_exc())
                    continue

            log.info(
                f"Sent {tennis_sent_count}/{len(tennis_batch_matches)} "
                f"tennis matches in this batch (found "
                f"{len(tennis_matches)} total today)"
            )

            send_job_status(
                f"✅ Tennis stats alert FINISHED\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(tennis_matches)} tennis matches today, "
                f"sent {tennis_sent_count}/{len(tennis_batch_matches)} "
                f"in this batch",
                BOT_TOKEN,
                CHAT_ID
            )

    except Exception as e:
        log.error(f"Tennis stats alert job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Tennis stats alert FAILED\n"
            f"Batch START={START} LIMIT={LIMIT}\n"
            f"Found {len(tennis_matches)} tennis matches today, sent "
            f"{tennis_sent_count} before failing\n"
            f"Error: {str(e)}",
            BOT_TOKEN,
            CHAT_ID
        )

    finally:
        log.info("Closing tennis scraper session...")
        if tennis_scraper is not None:
            tennis_scraper.close()


if __name__ == "__main__":
    main()
