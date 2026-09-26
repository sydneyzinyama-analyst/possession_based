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
LOG_PATH = os.getenv("SCRAPER_LOG_PATH", "flashscore_possession_dominance_alert.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("flashscore_possession_dominance")


# ---------------- TUNABLES ----------------
# Same 365scores.com JSON API (webws.365scores.com/web/...) as every
# earlier version of this scraper — plain, unauthenticated
# requests.get() calls work directly against it, no browser/session
# needed.
REQUEST_TIMEOUT_SEC = int(os.getenv("SCRAPER_REQUEST_TIMEOUT_SEC", "20"))
MAX_RETRIES = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))
RETRY_BACKOFF_SEC = float(os.getenv("SCRAPER_RETRY_BACKOFF_SEC", "1.5"))
DISCOVER_TIME_BUDGET_SEC = int(os.getenv("SCRAPER_DISCOVER_BUDGET_SEC", "60"))

# How many of each team's own recent finished matches to analyze.
TEAM_SAMPLE_MATCHES = 6

# How many of each tennis player's own recent finished singles matches
# to analyze — larger than TEAM_SAMPLE_MATCHES since tour players get
# through matches faster than football teams play fixtures.
TENNIS_SAMPLE_MATCHES = 10


# ---------------- JOB STATUS TELEGRAM ----------------
def send_job_status(message, bot_token, chat_id):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        requests.post(url, data=payload, timeout=20)
    except Exception as e:
        log.warning(f"Failed to send job status to Telegram: {e}")


# ---------------- SCRAPER CLASS ----------------
class SixtyFiveScoresScraper:
    """
    Talks directly to 365scores.com's own internal JSON API
    (webws.365scores.com/web/...) with plain requests calls — no
    browser, no session establishment, no fingerprinting needed. Data
    layer unchanged from earlier versions of this script — this
    filter only needs the regular per-team averages (possession,
    shots, big chances, xG, ...), so the form-record/latest-match-
    readout helpers earlier versions added for other filters were
    dropped as unused.
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
        self.team_id = None
        self.team_name = None

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
                if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                    log.warning(
                        f"discover_matches hit its "
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

                matches.append({
                    "id": g["id"],
                    "home_id": home["id"],
                    "home_name": home.get("name", ""),
                    "away_id": away["id"],
                    "away_name": away.get("name", ""),
                    "tournament": g.get("competitionDisplayName", ""),
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
        for stat_key in set(self.STAT_NAME_MAP.values()) | {"goals_prevented"}:
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

    def calculate_team_goals(self, results, team_id):
        total_goals = 0
        matches_counted = 0

        for r in results:
            if r.get("home_id") == team_id:
                total_goals += r.get("home_goals") or 0
                matches_counted += 1
            elif r.get("away_id") == team_id:
                total_goals += r.get("away_goals") or 0
                matches_counted += 1

        avg_goals = (
            total_goals / matches_counted if matches_counted > 0 else 0
        )

        return {
            "team": self.team_name or str(team_id),
            "total_goals": total_goals,
            "avg_goals": round(avg_goals, 2),
            "matches": matches_counted,
        }

    # ---------------- SCRAPER ----------------
    def analyze_team(self, team_id, team_name=None):
        t0 = time.time()
        self.team_id = team_id
        self.team_name = team_name or str(team_id)

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
            f"analyze_team({self.team_name!r}): {len(results)}/"
            f"{TEAM_SAMPLE_MATCHES} matches fetched in "
            f"{time.time()-t0:.1f}s total"
        )

        stats = self.calculate_team_goals(results, team_id)
        avg_gc = self._team_stat_avg(results, "goals", team_id, "against") or 0
        avg_xg = self._team_stat_avg(results, "xg", team_id, "for")
        avg_xga = self._team_stat_avg(results, "xg", team_id, "against")

        avg_gd = round(stats["avg_goals"] - avg_gc, 2)

        if avg_xg is not None and avg_xga is not None:
            avg_xgd = round(avg_xg - avg_xga, 2)
        else:
            avg_xgd = None

        stats.update({
            "avg_gc": avg_gc,
            "avg_gd": avg_gd,
            "avg_xg": avg_xg,
            "avg_xga": avg_xga,
            "avg_xgd": avg_xgd,
            "avg_corners_for": self._team_stat_avg(results, "corners", team_id, "for"),
            "avg_corners_against": self._team_stat_avg(results, "corners", team_id, "against"),
            "avg_big_chances_for": self._team_stat_avg(results, "big_chances", team_id, "for"),
            "avg_big_chances_against": self._team_stat_avg(results, "big_chances", team_id, "against"),
            "avg_yellow_cards": self._team_stat_avg(results, "yellow_cards", team_id, "for"),
            "avg_fouls": self._team_stat_avg(results, "fouls", team_id, "for"),
            "avg_xgot_for": self._team_stat_avg(results, "xgot", team_id, "for"),
            "avg_xgot_against": self._team_stat_avg(results, "xgot", team_id, "against"),
            "avg_goals_prevented": self._team_stat_avg(results, "goals_prevented", team_id, "for"),
            "avg_shots_for": self._team_stat_avg(results, "shots", team_id, "for"),
            "avg_shots_against": self._team_stat_avg(results, "shots", team_id, "against"),
            "avg_sot_for": self._team_stat_avg(results, "shots_on_target", team_id, "for"),
            "avg_sot_against": self._team_stat_avg(results, "shots_on_target", team_id, "against"),
            "avg_possession": self._team_stat_avg(results, "possession", team_id, "for"),
        })

        return {
            "team": stats["team"],
            "team_id": team_id,
            "matches": [m["id"] for m in recent],
            "results": results,
            "stats": stats,
        }

    # ---------------- TELEGRAM ----------------
    def send_telegram_message(self, message, bot_token, chat_id):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            }
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code != 200:
                log.warning(f"Telegram error: {r.text}")
        except Exception as e:
            log.error(f"Failed to send Telegram message: {e}")

    def close(self):
        try:
            self.session.close()
        except Exception as e:
            log.warning(f"Error closing session: {e}")


# ---------------- TENNIS SCRAPER CLASS ----------------
# Same webws.365scores.com API as SixtyFiveScoresScraper, sports=3
# instead of sports=1. Kept as a fully standalone class rather than
# sharing code with the football scraper above, even though
# _api_get/__init__/close duplicate it near-verbatim — the football
# scraper is proven, scheduled, production code, and refactoring it
# into a shared base class to save a little duplication isn't worth
# the risk of subtly breaking it.
#
# "score" here is SETS WON (e.g. 2-1, 3-0), not goals, and matches
# aren't a fixed length (best-of-3 vs best-of-5) — win_rate is the
# more directly comparable number across a mix of match lengths than
# raw average sets won. game/stats/ values are mostly compound strings
# like "3/8 (38%)" (made/attempted (pct%)), not football's plain
# numbers — _parse_stat_value extracts the percentage, the part that's
# actually comparable across players who serve/return a different
# number of points in a match.
#
# Doubles matches show up in the same feed, distinguishable only by a
# "/" joining two names in the competitor name (e.g. "Bucsa
# C./Melichar-Martinez N.") — no separate flag from the API.
# discover_matches and get_player_recent_matches default to
# singles_only=True and use that "/" check to skip them, since a
# doubles pairing's stats/history don't mean the same thing as a
# single player's.
class TennisScraper:
    """
    Same webws.365scores.com API as SixtyFiveScoresScraper, sports=3
    instead of sports=1 — see this section's header comment for what's
    actually different about tennis's data shape.
    """

    BASE_URL = "https://webws.365scores.com/web"
    SPORT_ID = 3

    COMMON_PARAMS = {
        "appTypeId": 5,
        "langId": 10,
        "timezoneName": "Africa/Johannesburg",
        "userCountryId": 134,
    }

    # Every stat name game/stats/ returns for a real finished singles
    # match, confirmed by direct inspection.
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
        "Max Points In a Row": "max_points_in_a_row",
        "Max Games In a Row": "max_games_in_a_row",
        "Points won in last 10": "points_won_last_10",
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
        self.player_id = None
        self.player_name = None

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

    @staticmethod
    def _is_doubles(home_name, away_name):
        return "/" in (home_name or "") or "/" in (away_name or "")

    # ---------------- DISCOVERY ----------------
    def discover_matches(
        self, target_count, date_str=None, only_upcoming=False,
        singles_only=True,
    ):
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
                    "home_sets": home.get("score"),
                    "away_sets": away.get("score"),
                    "start_time": g.get("startTime"),
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

    # ---------------- PLAYER HISTORY ----------------
    @staticmethod
    def _extract_rank(competitor):
        # competitor["rankings"] is a list like
        # [{"name": "ATP", "position": 245}] — present on roughly half
        # of entries (missing for players outside the computer
        # rankings). None-safe throughout.
        rankings = (competitor or {}).get("rankings")
        if not rankings:
            return None
        position = rankings[0].get("position")
        return position if isinstance(position, (int, float)) else None

    # A real singles match, sanity-bounded: nothing shorter than a
    # quick straight-sets bagel (~15 min) or longer than a marathon
    # 3-setter (~6h) — anything outside this is data noise, not a real
    # duration. See _parse_duration_string for why this bound exists.
    MIN_PLAUSIBLE_DURATION_MINUTES = 15
    MAX_PLAUSIBLE_DURATION_MINUTES = 360

    @staticmethod
    def _parse_duration_string(raw):
        # 365scores' "Sets" stage time field is ambiguous, confirmed
        # by direct inspection: it's USUALLY elapsed match duration in
        # "45'" (minutes) or "02:40" (H:MM) form — but it can ALSO be
        # a wall-clock time-of-day in that same H:MM shape (seen live:
        # "18:57" — clearly not a real match duration). There's no way
        # to tell the two apart from the string alone, so anything
        # that parses outside the plausible-duration bounds is treated
        # as a misparse and discarded rather than guessed at.
        if not raw:
            return None
        raw = str(raw).strip()
        try:
            if ":" in raw:
                hours, minutes = raw.split(":")
                value = int(hours) * 60 + int(minutes)
            else:
                value = int(raw.rstrip("'"))
        except (TypeError, ValueError):
            return None

        if not (
            TennisScraper.MIN_PLAUSIBLE_DURATION_MINUTES
            <= value
            <= TennisScraper.MAX_PLAUSIBLE_DURATION_MINUTES
        ):
            return None

        return value

    @staticmethod
    def _parse_match_duration(stages):
        """
        Extracts elapsed match duration in minutes from a finished
        match's raw `stages` list — confirmed by direct inspection:
        alongside individual "Set N" entries, one summary entry named
        "Sets" carries the match's total elapsed time. Returns None if
        `stages` is missing or nothing parses — never raises.
        """
        for stage in stages or []:
            if stage.get("name") == "Sets":
                return TennisScraper._parse_duration_string(stage.get("time"))
        return None

    def get_player_recent_matches(self, player_id, count=10, singles_only=True):
        """
        Returns up to `count` of this player's most recent FINISHED
        singles matches, each a light dict (id, home_id, home_name,
        away_id, away_name, home_sets, away_sets, home_rank, away_rank,
        match_duration_minutes, start_time). Most-recent-first, same
        as games/results/ for football.
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
                "match_duration_minutes": self._parse_match_duration(g.get("stages")),
                "start_time": g.get("startTime"),
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

    # ---------------- STAT AVERAGING ----------------
    def _player_stat_avg(self, results, stat_name, player_id, side="for"):
        total = 0
        counted = 0

        for r in results:
            is_home = r.get("home_id") == player_id
            is_away = r.get("away_id") == player_id
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

    def calculate_player_record(self, results, player_id):
        """
        Win rate and average sets won/conceded per match, plus
        avg_beaten_opponent_rank (average ATP/WTA rank of opponents
        actually beaten — lower is tougher; None if no wins in the
        sample had a ranked opponent, not 0, since 0 would misleadingly
        read as "beat the world #1s").

        Also carries last_match_dominant_win (True only if `results`'
        first entry — most-recent-first — was itself a straight-sets
        win; None if the window is empty or that match's own data is
        unusable), and last_match_duration_minutes/last_match_start_time
        straight off that same most-recent match — used by the signal
        engine as a current-form check and a fatigue risk factor,
        distinct from the 10-match aggregate (a player can satisfy
        every window-wide bar and still have just lost, or barely
        survived, their most recent outing).
        """
        wins = 0
        total_sets_won = 0
        total_sets_lost = 0
        matches_counted = 0
        beaten_opponent_ranks = []
        last_match_dominant_win = None
        last_match_duration_minutes = None
        last_match_start_time = None

        for i, r in enumerate(results):
            if r.get("home_id") == player_id:
                sets_won, sets_lost = r.get("home_sets"), r.get("away_sets")
                opponent_rank = r.get("away_rank")
            elif r.get("away_id") == player_id:
                sets_won, sets_lost = r.get("away_sets"), r.get("home_sets")
                opponent_rank = r.get("home_rank")
            else:
                continue

            if sets_won is None or sets_lost is None:
                continue

            total_sets_won += sets_won
            total_sets_lost += sets_lost
            won = sets_won > sets_lost
            if won:
                wins += 1
                if opponent_rank is not None:
                    beaten_opponent_ranks.append(opponent_rank)
            matches_counted += 1

            if i == 0:
                last_match_dominant_win = won and sets_lost == 0
                last_match_duration_minutes = r.get("match_duration_minutes")
                last_match_start_time = r.get("start_time")

        win_rate = wins / matches_counted if matches_counted > 0 else 0
        avg_sets_won = (
            total_sets_won / matches_counted if matches_counted > 0 else 0
        )
        avg_sets_lost = (
            total_sets_lost / matches_counted if matches_counted > 0 else 0
        )
        avg_beaten_opponent_rank = (
            round(sum(beaten_opponent_ranks) / len(beaten_opponent_ranks), 1)
            if beaten_opponent_ranks
            else None
        )

        return {
            "player": self.player_name or str(player_id),
            "wins": wins,
            "win_rate": round(win_rate, 2),
            "avg_sets_won": round(avg_sets_won, 2),
            "avg_sets_lost": round(avg_sets_lost, 2),
            "avg_beaten_opponent_rank": avg_beaten_opponent_rank,
            "last_match_dominant_win": last_match_dominant_win,
            "last_match_duration_minutes": last_match_duration_minutes,
            "last_match_start_time": last_match_start_time,
            "matches": matches_counted,
        }

    # ---------------- SCRAPER ----------------
    def analyze_player(self, player_id, player_name=None):
        t0 = time.time()
        self.player_id = player_id
        self.player_name = player_name or str(player_id)

        recent = self.get_player_recent_matches(player_id, count=TENNIS_SAMPLE_MATCHES)
        results = []
        for m in recent:
            match_stats = self.get_match_statistics(
                m["id"], m["home_id"], m["away_id"]
            )
            match_data = dict(m)
            match_data.update(match_stats)
            results.append(match_data)

        log.info(
            f"analyze_player({self.player_name!r}): {len(results)}/"
            f"{TENNIS_SAMPLE_MATCHES} matches fetched in "
            f"{time.time()-t0:.1f}s total"
        )

        stats = self.calculate_player_record(results, player_id)

        for stat_key in set(self.STAT_NAME_MAP.values()):
            stats[f"avg_{stat_key}_for"] = self._player_stat_avg(
                results, stat_key, player_id, "for"
            )
            stats[f"avg_{stat_key}_against"] = self._player_stat_avg(
                results, stat_key, player_id, "against"
            )

        return {
            "player": stats["player"],
            "player_id": player_id,
            "matches": [m["id"] for m in recent],
            "results": results,
            "stats": stats,
        }

    # ---------------- TELEGRAM ----------------
    def send_telegram_message(self, message, bot_token, chat_id):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            }
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code != 200:
                log.warning(f"Telegram error: {r.text}")
        except Exception as e:
            log.error(f"Failed to send Telegram message: {e}")

    def close(self):
        try:
            self.session.close()
        except Exception as e:
            log.warning(f"Error closing session: {e}")


# ---------------- HOME DOMINANCE SIGNAL ENGINE ----------------
#
# One-directional on purpose: this only ever backs the HOME side, not
# "whichever side is better" — the away side is never checked as the
# favourite here. The claim itself is simple and absolute: the home
# team has to be AHEAD OF the away team on every single department
# this scraper tracks (goals, defense, shots, shots on target, big
# chances for/against, corners, possession, and xG/xGA when both sides
# have it) — no partial credit, no corroboration score, no ratio math.
# One stat going the other way and it doesn't fire.
#
# NOTE ON CONFIDENCE: same disclaimer as every version of this file —
# "better in every department" is a heuristic read of the raw
# averages, not a measured probability.

MIN_SAMPLE_MATCHES = TEAM_SAMPLE_MATCHES

# Every department the home team must win outright against the away
# team's own average — (stat key, "gt" home must be higher / "lt"
# home must be lower).
DEPARTMENTS = [
    ("avg_goals", "gt"),
    ("avg_gc", "lt"),
    ("avg_shots_for", "gt"),
    ("avg_sot_for", "gt"),
    ("avg_big_chances_for", "gt"),
    ("avg_big_chances_against", "lt"),
    ("avg_corners_for", "gt"),
    ("avg_possession", "gt"),
]


def _escape_markdown(text):
    """
    Minimal escaping for Telegram's legacy "Markdown" parse mode: only
    _, *, ` and [ need escaping there.
    """
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def _home_better_in_every_department(home, away):
    """
    True only if the home side is ahead of the away side on every
    single entry in DEPARTMENTS, AND on xG/xGA too whenever both sides
    have that data (skipped, not required, only when genuinely missing
    — common on minor-league matches). Fails closed on any missing
    department stat DEPARTMENTS itself requires — a stat that can't be
    compared can't corroborate "better in every department".
    """
    for key, op in DEPARTMENTS:
        h_val = home.get(key)
        a_val = away.get(key)
        if h_val is None or a_val is None:
            return False
        if op == "gt" and not (h_val > a_val):
            return False
        if op == "lt" and not (h_val < a_val):
            return False

    h_xg, a_xg = home.get("avg_xg"), away.get("avg_xg")
    h_xga, a_xga = home.get("avg_xga"), away.get("avg_xga")

    if h_xg is not None and a_xg is not None and not (h_xg > a_xg):
        return False

    if h_xga is not None and a_xga is not None and not (h_xga < a_xga):
        return False

    return True


def evaluate_home_dominance_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if the HOME side is ahead of the
    away side in every tracked department (see
    _home_better_in_every_department), or None otherwise. Away teams
    are never backed by this signal — home-only, by design.
    """
    home = _escape_markdown(home)
    away = _escape_markdown(away)

    hs = home_data["stats"]
    as_ = away_data["stats"]

    if (
        hs.get("matches", 0) < MIN_SAMPLE_MATCHES
        or as_.get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    if not _home_better_in_every_department(hs, as_):
        return None

    # -------------------------------------------------
    # RISK FACTORS (shown, don't block the prediction)
    # -------------------------------------------------

    risks = []

    h_xg = hs.get("avg_xg")
    h_g = hs.get("avg_goals", 0)
    if h_xg is not None and h_g >= h_xg + 1.0:
        risks.append(
            f"{home} has been scoring above its own underlying chance "
            f"quality (goals {h_g} vs xG {h_xg}) — some regression "
            f"toward the mean is possible"
        )

    a_xg = as_.get("avg_xg")
    if a_xg is not None and a_xg >= 1.0:
        risks.append(
            f"{away} still averages {a_xg} xG/match despite trailing "
            f"in every department — capable of making the few chances "
            f"they get count"
        )

    # -------------------------------------------------
    # MESSAGE
    # -------------------------------------------------

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"🏠 *{home} vs {away}*",
        "",
        f"🎯 *Prediction: {home} (home) is better than {away} in "
        f"every department*",
        "",
        "📊 *Stats*",
        f"{home}   G {fmt(hs.get('avg_goals'))} | "
        f"GA {fmt(hs.get('avg_gc'))} | "
        f"xG {fmt(hs.get('avg_xg'))} | "
        f"xGA {fmt(hs.get('avg_xga'))} | "
        f"Shots {fmt(hs.get('avg_shots_for'))} | "
        f"SoT {fmt(hs.get('avg_sot_for'))} | "
        f"Poss {fmt(hs.get('avg_possession'))}%",
        f"{away}   G {fmt(as_.get('avg_goals'))} | "
        f"GA {fmt(as_.get('avg_gc'))} | "
        f"xG {fmt(as_.get('avg_xg'))} | "
        f"xGA {fmt(as_.get('avg_xga'))} | "
        f"Shots {fmt(as_.get('avg_shots_for'))} | "
        f"SoT {fmt(as_.get('avg_sot_for'))} | "
        f"Poss {fmt(as_.get('avg_possession'))}%",
        f"BigCh {fmt(hs.get('avg_big_chances_for'))}/{fmt(hs.get('avg_big_chances_against'))} vs "
        f"{fmt(as_.get('avg_big_chances_for'))}/{fmt(as_.get('avg_big_chances_against'))} | "
        f"Corners {fmt(hs.get('avg_corners_for'))} vs {fmt(as_.get('avg_corners_for'))}",
        "",
    ]

    if risks:
        lines.append(f"⚠️ *Risk factors ({len(risks)})*")
        lines.extend(f"• {r}" for r in risks)
        lines.append("")

    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


# ---------------- TENNIS NO-LOSS SIGNAL ENGINE ----------------
#
# The tennis analog of what a "clear favourite" alert should claim:
# not just "this player is likely to win", but "this player is such a
# clear step above their opponent right now that a loss here would be
# a massive shock". Reads TennisScraper.analyze_player's output dict
# ({"player", "matches", "results", "stats"}).
#
# Same two-part shape as every signal in this script's history: a hard
# gate (the player has to be ahead by a REAL margin, not barely, on
# win rate and every points-won stat — see _stronger_player_on_all_fronts)
# and a corroboration score on top of it (see _player_edge_score) that
# has to clear its own bar before this fires. One function checks both
# directions (either player could be the one being backed) since the
# API's "home"/"away" here is just listing order, not a real asymmetry
# like a football home crowd — no reason to duplicate the whole
# function just to swap which side is backed.
#
# NOTE ON CONFIDENCE: same disclaimer as every signal in this file —
# every threshold below is a heuristic cutoff, not a measured
# probability.

TENNIS_MIN_SAMPLE_MATCHES = TENNIS_SAMPLE_MATCHES

# "Clear favourite" gate — deliberately not just "ahead", but ahead BY
# A REAL MARGIN on every front. Win rate is in tenths over a 10-match
# sample, so these are chosen with that granularity in mind:
# TENNIS_MIN_WIN_RATE=0.6 requires the favourite to have won at least
# 6 of their last 10; TENNIS_WIN_RATE_GAP=0.6 requires the opponent to
# be at least 60 percentage points behind (e.g. 1.0 vs 0.4, 0.8 vs
# 0.2, 0.6 vs 0.0) — a materially bigger gap than "technically ahead".
TENNIS_MIN_WIN_RATE = 0.6
TENNIS_WIN_RATE_GAP = 0.6

# Minimum percentage-point gap required on total/service points won.
# Break points gets a wider buffer since it's a noisier per-match
# count (far fewer break points than total points played in a match,
# so the percentage swings harder on small samples).
TENNIS_POINTS_GAP_BUFFER = 3.0
TENNIS_BREAK_POINTS_GAP_BUFFER = 5.0

# Corroboration bar on top of the hard gate. Max achievable is 5.5 (1
# net aces/double-faults + 1 first-serve% + 1 second-serve% + 1
# games-won% + 1 beaten-opponent-rank + 0.5 tiebreaks-adjacent last-
# match-dominant-win); set well past half to match the hard gate's own
# tightening — "clear favourite, loss would be a shock" should mean
# most of the corroborating stats agree, not just over half.
TENNIS_EDGE_SCORE_THRESHOLD = 4.0

# Fatigue risk factor — shown, doesn't block the prediction. Flags
# when the player being backed has averaged notably longer recent
# matches than the player they're facing.
TENNIS_FATIGUE_RISK_MINUTES = 20


def _stronger_player_on_all_fronts(
    win_rate, opp_win_rate,
    total_points_won_pct, opp_total_points_won_pct,
    service_points_won_pct, opp_service_points_won_pct,
    break_points_won_pct, opp_break_points_won_pct,
):
    """
    True only if "player" is a CLEAR favourite over "opp" — ahead by a
    real margin on win rate (and the gap over opp's own win rate),
    plus winning total points, service points, and break points each
    by their own buffer. No partial credit. Fails closed on any
    missing input.
    """
    if None in (
        win_rate, opp_win_rate,
        total_points_won_pct, opp_total_points_won_pct,
        service_points_won_pct, opp_service_points_won_pct,
        break_points_won_pct, opp_break_points_won_pct,
    ):
        return False

    return (
        win_rate >= TENNIS_MIN_WIN_RATE
        and (win_rate - opp_win_rate) >= TENNIS_WIN_RATE_GAP
        and (total_points_won_pct - opp_total_points_won_pct) >= TENNIS_POINTS_GAP_BUFFER
        and (service_points_won_pct - opp_service_points_won_pct) >= TENNIS_POINTS_GAP_BUFFER
        and (break_points_won_pct - opp_break_points_won_pct) >= TENNIS_BREAK_POINTS_GAP_BUFFER
    )


def _player_edge_score(
    aces_for, double_faults_for, opp_aces_for, opp_double_faults_for,
    first_serve_pct, opp_first_serve_pct,
    second_serve_pct, opp_second_serve_pct,
    games_won_pct, opp_games_won_pct,
    avg_beaten_opponent_rank, opp_avg_beaten_opponent_rank,
    last_match_dominant_win,
):
    """
    Extra corroboration on top of the hard gate — net free points off
    serve (aces minus double faults), first- and second-serve points
    won, total games won, strength of opposition beaten (lower
    ATP/WTA rank number = tougher — see calculate_player_record), and
    whether the player won their most recent single match in straight
    sets. Every comparison is a plain > (or < for rank) check worth
    partial credit, not a hard requirement. All args None-safe.
    """
    score = 0.0

    if None not in (aces_for, double_faults_for, opp_aces_for, opp_double_faults_for):
        net = aces_for - double_faults_for
        opp_net = opp_aces_for - opp_double_faults_for
        if net > opp_net:
            score += 1

    if first_serve_pct is not None and opp_first_serve_pct is not None and first_serve_pct > opp_first_serve_pct:
        score += 1

    if second_serve_pct is not None and opp_second_serve_pct is not None and second_serve_pct > opp_second_serve_pct:
        score += 1

    if games_won_pct is not None and opp_games_won_pct is not None and games_won_pct > opp_games_won_pct:
        score += 1

    if (
        avg_beaten_opponent_rank is not None
        and opp_avg_beaten_opponent_rank is not None
        and avg_beaten_opponent_rank < opp_avg_beaten_opponent_rank
    ):
        score += 1

    if last_match_dominant_win:
        score += 0.5

    return score


def evaluate_tennis_no_loss_signal(player_a, player_b, a_data, b_data, m_url):
    """
    Returns a Telegram-ready message if either player is a CLEAR
    favourite over the other (see _stronger_player_on_all_fronts) AND
    clears TENNIS_EDGE_SCORE_THRESHOLD worth of corroboration (see
    _player_edge_score), or None if neither direction clears both
    bars. a_data/b_data are TennisScraper.analyze_player's output
    dicts; player_a/player_b are display names for the message.
    """
    player_a = _escape_markdown(player_a)
    player_b = _escape_markdown(player_b)

    a_stats = a_data["stats"]
    b_stats = b_data["stats"]

    if (
        a_stats.get("matches", 0) < TENNIS_MIN_SAMPLE_MATCHES
        or b_stats.get("matches", 0) < TENNIS_MIN_SAMPLE_MATCHES
    ):
        return None

    def edge(front_stats, back_stats):
        """
        Checks front_stats's side (the one being backed) against
        back_stats — the hard gate plus the corroboration score.
        Returns the score if the hard gate passes, else None.
        """
        if not _stronger_player_on_all_fronts(
            front_stats.get("win_rate"), back_stats.get("win_rate"),
            front_stats.get("avg_total_points_won_pct_for"), back_stats.get("avg_total_points_won_pct_for"),
            front_stats.get("avg_service_points_won_pct_for"), back_stats.get("avg_service_points_won_pct_for"),
            front_stats.get("avg_break_points_won_pct_for"), back_stats.get("avg_break_points_won_pct_for"),
        ):
            return None

        return _player_edge_score(
            front_stats.get("avg_aces_for"), front_stats.get("avg_double_faults_for"),
            back_stats.get("avg_aces_for"), back_stats.get("avg_double_faults_for"),
            front_stats.get("avg_first_serve_points_won_pct_for"), back_stats.get("avg_first_serve_points_won_pct_for"),
            front_stats.get("avg_second_serve_points_won_pct_for"), back_stats.get("avg_second_serve_points_won_pct_for"),
            front_stats.get("avg_total_games_won_pct_for"), back_stats.get("avg_total_games_won_pct_for"),
            front_stats.get("avg_beaten_opponent_rank"), back_stats.get("avg_beaten_opponent_rank"),
            front_stats.get("last_match_dominant_win"),
        )

    a_score = edge(a_stats, b_stats)
    winner, loser, winner_name, loser_name, score = None, None, None, None, None

    if a_score is not None and a_score >= TENNIS_EDGE_SCORE_THRESHOLD:
        winner, loser, winner_name, loser_name, score = a_stats, b_stats, player_a, player_b, a_score
    else:
        b_score = edge(b_stats, a_stats)
        if b_score is not None and b_score >= TENNIS_EDGE_SCORE_THRESHOLD:
            winner, loser, winner_name, loser_name, score = b_stats, a_stats, player_b, player_a, b_score

    if winner is None:
        return None

    # -------------------------------------------------
    # RISK FACTORS (shown, don't block the prediction)
    # -------------------------------------------------

    risks = []

    winner_duration = winner.get("last_match_duration_minutes")
    loser_duration = loser.get("last_match_duration_minutes")
    if (
        winner_duration is not None
        and loser_duration is not None
        and winner_duration - loser_duration >= TENNIS_FATIGUE_RISK_MINUTES
    ):
        risks.append(
            f"{winner_name}'s most recent match ran {winner_duration:.0f} "
            f"min vs {loser_name}'s {loser_duration:.0f} min — the "
            f"tougher recent workload could be a fatigue factor here"
        )

    # -------------------------------------------------
    # MESSAGE
    # -------------------------------------------------

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"🎾 *{player_a} vs {player_b}*",
        "",
        f"🔒 *Prediction: {winner_name} should NOT lose this match* "
        f"— a loss here would be a massive shock",
        f"Won ≥{TENNIS_MIN_WIN_RATE:.0%} of, and at least "
        f"{TENNIS_WIN_RATE_GAP:.0%} more of, their last "
        f"{TENNIS_MIN_SAMPLE_MATCHES} matches than {loser_name} — "
        f"clear margins on total/service/break points won too — "
        f"corroboration score {score:.1f} "
        f"(bar: {TENNIS_EDGE_SCORE_THRESHOLD:.1f})",
        "",
        "📊 *Stats*",
        f"{winner_name}   Win rate {fmt(winner.get('win_rate'))} | "
        f"Total pts won {fmt(winner.get('avg_total_points_won_pct_for'))}% | "
        f"Svc pts won {fmt(winner.get('avg_service_points_won_pct_for'))}% | "
        f"BP won {fmt(winner.get('avg_break_points_won_pct_for'))}%",
        f"{loser_name}   Win rate {fmt(loser.get('win_rate'))} | "
        f"Total pts won {fmt(loser.get('avg_total_points_won_pct_for'))}% | "
        f"Svc pts won {fmt(loser.get('avg_service_points_won_pct_for'))}% | "
        f"BP won {fmt(loser.get('avg_break_points_won_pct_for'))}%",
        f"Aces/DF {fmt(winner.get('avg_aces_for'))}/{fmt(winner.get('avg_double_faults_for'))} vs "
        f"{fmt(loser.get('avg_aces_for'))}/{fmt(loser.get('avg_double_faults_for'))}",
        f"1st/2nd serve won {fmt(winner.get('avg_first_serve_points_won_pct_for'))}%/"
        f"{fmt(winner.get('avg_second_serve_points_won_pct_for'))}% vs "
        f"{fmt(loser.get('avg_first_serve_points_won_pct_for'))}%/"
        f"{fmt(loser.get('avg_second_serve_points_won_pct_for'))}%",
        f"Games won {fmt(winner.get('avg_total_games_won_pct_for'))}% vs "
        f"{fmt(loser.get('avg_total_games_won_pct_for'))}%",
        f"Avg rank of opponents beaten: {fmt(winner.get('avg_beaten_opponent_rank'))} vs "
        f"{fmt(loser.get('avg_beaten_opponent_rank'))} (lower = tougher)",
        "",
    ]

    if risks:
        lines.append(f"⚠️ *Risk factors ({len(risks)})*")
        lines.extend(f"• {r}" for r in risks)
        lines.append("")

    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


# ---------------- ALERT SCRIPT ----------------

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        type=int,
        default=0
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=100
    )

    args = parser.parse_args()

    START = max(0, args.start)
    LIMIT = max(1, args.limit)

    TARGET_COUNT = START + LIMIT

    BOT_TOKEN = os.getenv(
        "BOT_TOKEN",
        ""
    ).strip()

    CHAT_ID = os.getenv(
        "CHAT_ID",
        ""
    ).strip()

    if not BOT_TOKEN or not CHAT_ID:
        log.error(
            "BOT_TOKEN or CHAT_ID is missing from environment variables."
        )
        return

    send_job_status(
        f"🚀 Job STARTED (soccer home-dominance + tennis no-loss alert)\n"
        f"Batch START={START} LIMIT={LIMIT}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting 365scores home-dominance alert script...")
    log.info(f"Batch start={START}, limit={LIMIT}")

    scraper = None
    matches = []
    analyzed_count = 0

    try:
        scraper = SixtyFiveScoresScraper()

        matches = scraper.discover_matches(
            TARGET_COUNT,
            only_upcoming=True
        )

        log.info(f"Found {len(matches)} upcoming matches total")

        batch_matches = matches[
            START:START + LIMIT
        ]

        log.info(
            f"This job will process {len(batch_matches)} matches "
            f"from {START} to {START + LIMIT - 1}"
        )

        if not batch_matches:

            log.info("No matches in this batch.")

            send_job_status(
                f"⚠️ Home-dominance alert FINISHED (No matches)\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, 0 fell in this "
                f"batch's range",
                BOT_TOKEN,
                CHAT_ID
            )

        else:

            for idx, match in enumerate(
                batch_matches,
                start=START + 1
            ):

                m_url = f"https://www.365scores.com/en-uk/football/game/{match['id']}"
                home = match["home_name"]
                away = match["away_name"]

                log.info(
                    f"Processing match {idx}: {home} vs {away} "
                    f"({match.get('tournament', '')}) {m_url}"
                )

                try:
                    if not home or not away:
                        log.warning(
                            "Could not extract teams, skipping match"
                        )
                        continue

                    home_error = None
                    away_error = None

                    try:
                        home_data = scraper.analyze_team(
                            match["home_id"], match["home_name"]
                        )
                    except Exception as e:
                        home_data = None
                        home_error = e

                    try:
                        away_data = scraper.analyze_team(
                            match["away_id"], match["away_name"]
                        )
                    except Exception as e:
                        away_data = None
                        away_error = e

                    if home_error:
                        log.error(f"Home team analysis failed: {home_error}")

                    if away_error:
                        log.error(f"Away team analysis failed: {away_error}")

                    if not home_data or not away_data:

                        log.warning(
                            "Could not analyze one or both teams, "
                            "skipping match"
                        )

                        continue

                    analyzed_count += 1

                    dominance_msg = evaluate_home_dominance_signal(
                        home,
                        away,
                        home_data,
                        away_data,
                        m_url
                    )

                    if dominance_msg:
                        log.info("ALERT (home dominance):\n" + dominance_msg)
                        scraper.send_telegram_message(
                            dominance_msg,
                            BOT_TOKEN,
                            CHAT_ID
                        )
                    else:
                        log.info("No home-dominance signal found.")

                except Exception as match_err:
                    log.error(
                        f"Error processing match {m_url}: {match_err}"
                    )
                    log.debug(traceback.format_exc())
                    continue

            log.info(
                f"Analyzed {analyzed_count}/{len(batch_matches)} matches "
                f"in this batch (found {len(matches)} total today)"
            )

            send_job_status(
                f"✅ Home-dominance alert FINISHED\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, analyzed "
                f"{analyzed_count}/{len(batch_matches)} in this batch",
                BOT_TOKEN,
                CHAT_ID
            )

    except Exception as e:

        log.error(f"Home-dominance alert job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Home-dominance alert FAILED\n"
            f"Batch START={START} LIMIT={LIMIT}\n"
            f"Found {len(matches)} matches today, analyzed "
            f"{analyzed_count} before failing\n"
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
    # of soccer's above: a tennis-side failure can't affect the soccer
    # results already sent, and if soccer's block above failed
    # instead, tennis still gets to run rather than being skipped
    # (this isn't nested inside soccer's try). Reuses START/LIMIT/
    # TARGET_COUNT/BOT_TOKEN/CHAT_ID from above — same batch window,
    # same destination chat.

    tennis_scraper = None
    tennis_matches = []
    tennis_analyzed_count = 0

    try:
        tennis_scraper = TennisScraper()

        tennis_matches = tennis_scraper.discover_matches(
            TARGET_COUNT,
            only_upcoming=True
        )

        log.info(f"Found {len(tennis_matches)} upcoming singles matches total")

        tennis_batch_matches = tennis_matches[
            START:START + LIMIT
        ]

        log.info(
            f"This job will process {len(tennis_batch_matches)} tennis "
            f"matches from {START} to {START + LIMIT - 1}"
        )

        if not tennis_batch_matches:

            log.info("No tennis matches in this batch.")

            send_job_status(
                f"⚠️ Tennis no-loss alert FINISHED (No matches)\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(tennis_matches)} matches today, 0 fell in "
                f"this batch's range",
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

                    a_error = None
                    b_error = None

                    try:
                        a_data = tennis_scraper.analyze_player(
                            match["home_id"], match["home_name"]
                        )
                    except Exception as e:
                        a_data = None
                        a_error = e

                    try:
                        b_data = tennis_scraper.analyze_player(
                            match["away_id"], match["away_name"]
                        )
                    except Exception as e:
                        b_data = None
                        b_error = e

                    if a_error:
                        log.error(f"Player A analysis failed: {a_error}")

                    if b_error:
                        log.error(f"Player B analysis failed: {b_error}")

                    if not a_data or not b_data:

                        log.warning(
                            "Could not analyze one or both players, "
                            "skipping match"
                        )

                        continue

                    tennis_analyzed_count += 1

                    no_loss_msg = evaluate_tennis_no_loss_signal(
                        player_a,
                        player_b,
                        a_data,
                        b_data,
                        m_url
                    )

                    if no_loss_msg:
                        log.info("ALERT (tennis no-loss):\n" + no_loss_msg)
                        tennis_scraper.send_telegram_message(
                            no_loss_msg,
                            BOT_TOKEN,
                            CHAT_ID
                        )
                    else:
                        log.info("No tennis no-loss signal found.")

                except Exception as match_err:
                    log.error(
                        f"Error processing tennis match {m_url}: {match_err}"
                    )
                    log.debug(traceback.format_exc())
                    continue

            log.info(
                f"Analyzed {tennis_analyzed_count}/{len(tennis_batch_matches)} "
                f"tennis matches in this batch (found {len(tennis_matches)} "
                f"total today)"
            )

            send_job_status(
                f"✅ Tennis no-loss alert FINISHED\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(tennis_matches)} matches today, analyzed "
                f"{tennis_analyzed_count}/{len(tennis_batch_matches)} in "
                f"this batch",
                BOT_TOKEN,
                CHAT_ID
            )

    except Exception as e:

        log.error(f"Tennis job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Tennis no-loss alert FAILED\n"
            f"Batch START={START} LIMIT={LIMIT}\n"
            f"Found {len(tennis_matches)} matches today, analyzed "
            f"{tennis_analyzed_count} before failing\n"
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
