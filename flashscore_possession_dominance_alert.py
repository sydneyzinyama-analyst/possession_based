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


# ---------------- UNDER 3.5 GOALS SIGNAL ENGINE ----------------
#
# Match-level, not team-directional: this asks whether the MATCH as a
# whole — combined home + away output — is projected low enough to
# trust "under 3.5 total goals", not which side is stronger. Each
# side's own projected output against this specific opponent uses the
# same blended for+against expected-goals estimate this script's
# earlier signals have used (xG-based when both sides have full
# xG/xGA data, a raw-goals fallback otherwise); the two are summed
# into one combined projection, which has to sit meaningfully below
# 3.5 — not just "any number under 3.5" — before this is trusted,
# since match-to-match variance means a projection of exactly 3.4 is
# a coinflip on actually finishing under the line, not a safe call.
#
# NOTE ON CONFIDENCE: same disclaimer as every version of this file —
# every threshold below is a heuristic cutoff, not a measured
# probability.

MIN_SAMPLE_MATCHES = TEAM_SAMPLE_MATCHES

# The market line this signal is actually about.
UNDER_GOALS_LINE = 3.5

# How far below UNDER_GOALS_LINE the combined projection needs to sit
# before it's trusted — the buffer that turns "technically under" into
# "comfortably under".
MAX_EXPECTED_COMBINED_GOALS = 2.6

# Sanity check on top of the projection: each side's own raw scoring
# average (regardless of opponent) has to independently support being
# a modest-scoring side — two prolific attacks projecting low against
# each other's specific defense is a weaker claim than two sides that
# are independently low-scoring in general.
OWN_GOALS_SANITY_MAX = 1.6

# Corroboration floors — used only in the scoring function below, not
# the hard gate, so their absence doesn't disqualify a match.
BOTH_DEFENSES_SOLID_MAX_CONCEDED = 1.2
COMBINED_SHOTS_MODEST_MAX = 22.0
COMBINED_BIG_CHANCES_MODEST_MAX = 2.5

# Corroboration bar on top of the hard gate. Max achievable is 4.0 (1
# home defense solid + 1 away defense solid + 1 combined shot volume
# modest + 1 combined big chances modest); set at half of that.
UNDER_GOALS_SCORE_THRESHOLD = 2.0


def _escape_markdown(text):
    """
    Minimal escaping for Telegram's legacy "Markdown" parse mode: only
    _, *, ` and [ need escaping there.
    """
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def _expected_goals(home, away):
    """
    Blended for+against expected-goals estimate for both sides: xG-
    based when both sides have full xG/xGA data, a raw-goals fallback
    otherwise. Returns (expected_home_goals, expected_away_goals,
    basis) — the one pair of numbers that already combines each
    side's own attacking rate with the OTHER side's own defensive
    leakiness, rather than reading either side's stats in isolation.
    """
    home_xg, away_xg = home.get("avg_xg"), away.get("avg_xg")
    home_xga, away_xga = home.get("avg_xga"), away.get("avg_xga")

    if None not in (home_xg, away_xg, home_xga, away_xga):
        expected_home = (home_xg + away_xga) / 2
        expected_away = (away_xg + home_xga) / 2
        basis = "xG-based"
    else:
        home_g, away_g = home.get("avg_goals", 0), away.get("avg_goals", 0)
        home_gc, away_gc = home.get("avg_gc", 0), away.get("avg_gc", 0)
        expected_home = (home_g + away_gc) / 2
        expected_away = (away_g + home_gc) / 2
        basis = "goals-based, no xG data"

    return expected_home, expected_away, basis


def _under_goals_score(home, away):
    """
    Corroboration score — every factor here is an extra, independent
    reason the low combined projection is genuinely a low-scoring
    match, not just an artifact of this one blended projection. All
    lookups None-safe.
    """
    score = 0.0

    home_gc = home.get("avg_gc")
    if home_gc is not None and home_gc <= BOTH_DEFENSES_SOLID_MAX_CONCEDED:
        score += 1

    away_gc = away.get("avg_gc")
    if away_gc is not None and away_gc <= BOTH_DEFENSES_SOLID_MAX_CONCEDED:
        score += 1

    home_shots = home.get("avg_shots_for")
    away_shots = away.get("avg_shots_for")
    if home_shots is not None and away_shots is not None:
        if (home_shots + away_shots) <= COMBINED_SHOTS_MODEST_MAX:
            score += 1

    home_bc = home.get("avg_big_chances_for")
    away_bc = away.get("avg_big_chances_for")
    if home_bc is not None and away_bc is not None:
        if (home_bc + away_bc) <= COMBINED_BIG_CHANCES_MODEST_MAX:
            score += 1

    return score


def evaluate_under_goals_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if the match's combined projected
    output (see _expected_goals) sits at or below
    MAX_EXPECTED_COMBINED_GOALS, each side's own raw scoring average
    independently supports that (OWN_GOALS_SANITY_MAX), and the match
    clears UNDER_GOALS_SCORE_THRESHOLD worth of corroboration (see
    _under_goals_score) — or None otherwise. Match-level, not
    directional — neither side is singled out as "the weak one".
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

    home_g = hs.get("avg_goals")
    away_g = as_.get("avg_goals")
    if home_g is None or away_g is None:
        return None
    if home_g > OWN_GOALS_SANITY_MAX or away_g > OWN_GOALS_SANITY_MAX:
        return None

    expected_home, expected_away, basis = _expected_goals(hs, as_)
    expected_combined = expected_home + expected_away

    if expected_combined > MAX_EXPECTED_COMBINED_GOALS:
        return None

    score = _under_goals_score(hs, as_)

    if score < UNDER_GOALS_SCORE_THRESHOLD:
        return None

    # -------------------------------------------------
    # RISK FACTORS (shown, don't block the prediction)
    # -------------------------------------------------

    risks = []

    home_xgot = hs.get("avg_xgot_for")
    home_xg = hs.get("avg_xg")
    if home_xgot is not None and home_xg is not None and home_xgot >= home_xg + 0.3:
        risks.append(
            f"{home} has been clinical when it does get a chance "
            f"(xGOT {home_xgot} vs xG {home_xg}) — capable of making a "
            f"low volume of shots count"
        )

    away_xgot = as_.get("avg_xgot_for")
    away_xg = as_.get("avg_xg")
    if away_xgot is not None and away_xg is not None and away_xgot >= away_xg + 0.3:
        risks.append(
            f"{away} has been clinical when it does get a chance "
            f"(xGOT {away_xgot} vs xG {away_xg}) — capable of making a "
            f"low volume of shots count"
        )

    home_corners = hs.get("avg_corners_for")
    away_corners = as_.get("avg_corners_for")
    if home_corners is not None and away_corners is not None:
        combined_corners = home_corners + away_corners
        if combined_corners >= 11.0:
            risks.append(
                f"Combined corners are fairly high ({combined_corners:.1f}) "
                f"— set-piece goal risk isn't fully captured by the "
                f"open-play projection above"
            )

    # -------------------------------------------------
    # MESSAGE
    # -------------------------------------------------

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"🔒 *{home} vs {away}*",
        "",
        f"🎯 *Prediction: Under {UNDER_GOALS_LINE} total goals*",
        f"Projected combined ~{expected_combined:.2f} "
        f"({home} ~{expected_home:.2f} + {away} ~{expected_away:.2f}, "
        f"{basis}) | corroboration score {score:.1f} "
        f"(bar: {UNDER_GOALS_SCORE_THRESHOLD:.1f})",
        "",
        "📊 *Stats*",
        f"{home}   G {fmt(hs.get('avg_goals'))} | "
        f"GA {fmt(hs.get('avg_gc'))} | "
        f"xG {fmt(hs.get('avg_xg'))} | "
        f"xGA {fmt(hs.get('avg_xga'))} | "
        f"Shots {fmt(hs.get('avg_shots_for'))} | "
        f"BigCh {fmt(hs.get('avg_big_chances_for'))}",
        f"{away}   G {fmt(as_.get('avg_goals'))} | "
        f"GA {fmt(as_.get('avg_gc'))} | "
        f"xG {fmt(as_.get('avg_xg'))} | "
        f"xGA {fmt(as_.get('avg_xga'))} | "
        f"Shots {fmt(as_.get('avg_shots_for'))} | "
        f"BigCh {fmt(as_.get('avg_big_chances_for'))}",
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
        f"🚀 Job STARTED (soccer under-3.5-goals alert)\n"
        f"Batch START={START} LIMIT={LIMIT}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting 365scores under-3.5-goals alert script...")
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
                f"⚠️ Under-3.5-goals alert FINISHED (No matches)\n"
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

                    under_goals_msg = evaluate_under_goals_signal(
                        home,
                        away,
                        home_data,
                        away_data,
                        m_url
                    )

                    if under_goals_msg:
                        log.info("ALERT (under 3.5 goals):\n" + under_goals_msg)
                        scraper.send_telegram_message(
                            under_goals_msg,
                            BOT_TOKEN,
                            CHAT_ID
                        )
                    else:
                        log.info("No under-3.5-goals signal found.")

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
                f"✅ Under-3.5-goals alert FINISHED\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, analyzed "
                f"{analyzed_count}/{len(batch_matches)} in this batch",
                BOT_TOKEN,
                CHAT_ID
            )

    except Exception as e:

        log.error(f"Under-3.5-goals alert job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Under-3.5-goals alert FAILED\n"
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



if __name__ == "__main__":
    main()
