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


# ---------------- WEAK AWAY TEAM SIGNAL ENGINE ----------------
#
# One-directional on purpose: this only ever backs the claim that the
# AWAY side is weak — the home side is never checked as the weak one.
# Two things have to both hold: the away side is projected to score
# 1 goal at most against this specific home defense (not just "fewer
# than the home side", an absolute ceiling), AND the home side
# projects a real edge over the away side (so "unlikely to win" isn't
# just a coin flip going the other way). "Projected" uses the same
# blended for+against expected-goals estimate this script's earlier
# signals have used — xG-based when both sides have full xG/xGA data,
# a raw-goals fallback otherwise — since a team's likely output
# against a SPECIFIC opponent depends on both that team's own attack
# and the opponent's own defense, not either alone.
#
# NOTE ON CONFIDENCE: same disclaimer as every version of this file —
# every threshold below is a heuristic cutoff, not a measured
# probability.

MIN_SAMPLE_MATCHES = TEAM_SAMPLE_MATCHES

# The literal claim: away's projected output against this home side
# has to sit at or below this — "unlikely to score more than 1".
AWAY_WEAK_MAX_EXPECTED_GOALS = 1.1

# Sanity check on top of the projection: away's own raw scoring
# average (regardless of opponent) has to independently support being
# a low-scoring side — a team that normally scores freely projecting
# low against one tough defense is a different, weaker claim than a
# team that barely scores at all.
AWAY_WEAK_MAX_OWN_GOALS = 1.2

# How much of a projected edge the home side needs over the away side
# for "away unlikely to win" to mean something beyond a marginal call.
HOME_EDGE_MIN_MARGIN = 0.3

# Corroboration floors — used only in the scoring function below, not
# the hard gate, so their absence doesn't disqualify a match.
AWAY_WEAK_MAX_XG = 1.0
AWAY_WEAK_MAX_SHOTS = 10.0
AWAY_WEAK_MAX_SOT = 4.0
AWAY_WEAK_MIN_OWN_CONCEDED = 1.3

# Corroboration bar on top of the hard gate. Max achievable is 4.0 (1
# away's own xG backs up the low-scoring profile + 1 away creates
# little shot volume + 1 away creates little on target + 1 away's own
# defense is leaky too, reinforcing "unlikely to win" beyond just not
# scoring); set at half of that.
AWAY_WEAK_SCORE_THRESHOLD = 2.0


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


def _away_weak_score(away):
    """
    Corroboration score — every factor here is an extra, independent
    reason the away side's low-scoring, unlikely-to-win profile is
    genuinely real rather than a one-off projection against this one
    matchup. All lookups None-safe.
    """
    score = 0.0

    away_xg = away.get("avg_xg")
    if away_xg is not None and away_xg <= AWAY_WEAK_MAX_XG:
        score += 1

    away_shots = away.get("avg_shots_for")
    if away_shots is not None and away_shots <= AWAY_WEAK_MAX_SHOTS:
        score += 1

    away_sot = away.get("avg_sot_for")
    if away_sot is not None and away_sot <= AWAY_WEAK_MAX_SOT:
        score += 1

    away_gc = away.get("avg_gc")
    if away_gc is not None and away_gc >= AWAY_WEAK_MIN_OWN_CONCEDED:
        score += 1

    return score


def evaluate_weak_away_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if the AWAY side projects at most
    AWAY_WEAK_MAX_EXPECTED_GOALS against this home side (see
    _expected_goals), away's own raw scoring average independently
    supports that (AWAY_WEAK_MAX_OWN_GOALS), the home side projects a
    real edge (HOME_EDGE_MIN_MARGIN), and the away side clears
    AWAY_WEAK_SCORE_THRESHOLD worth of corroboration (see
    _away_weak_score) — or None otherwise. One-directional by design:
    the home side is never checked as the weak one.
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

    away_g = as_.get("avg_goals")
    if away_g is None or away_g > AWAY_WEAK_MAX_OWN_GOALS:
        return None

    expected_home, expected_away, basis = _expected_goals(hs, as_)

    if expected_away > AWAY_WEAK_MAX_EXPECTED_GOALS:
        return None

    if (expected_home - expected_away) < HOME_EDGE_MIN_MARGIN:
        return None

    score = _away_weak_score(as_)

    if score < AWAY_WEAK_SCORE_THRESHOLD:
        return None

    # -------------------------------------------------
    # RISK FACTORS (shown, don't block the prediction)
    # -------------------------------------------------

    risks = []

    away_xg = as_.get("avg_xg")
    away_xgot = as_.get("avg_xgot_for")
    if away_xgot is not None and away_xg is not None and away_xgot >= away_xg + 0.3:
        risks.append(
            f"{away} has been clinical when it does get a chance "
            f"(xGOT {away_xgot} vs xG {away_xg}) — a low volume of "
            f"shots doesn't rule out one going in"
        )

    home_gc = hs.get("avg_gc")
    if home_gc is not None and home_gc >= 1.0:
        risks.append(
            f"{home}'s own defense isn't airtight either (GA "
            f"{home_gc}/match) — some room for {away} to nick a goal"
        )

    # -------------------------------------------------
    # MESSAGE
    # -------------------------------------------------

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"📉 *{home} vs {away}*",
        "",
        f"🎯 *Prediction: {away} (away) unlikely to win, and unlikely "
        f"to score more than 1 goal*",
        f"Projected {home} ~{expected_home:.2f} vs {away} "
        f"~{expected_away:.2f} ({basis}) | corroboration score "
        f"{score:.1f} (bar: {AWAY_WEAK_SCORE_THRESHOLD:.1f})",
        "",
        "📊 *Stats*",
        f"{away} (away)   G {fmt(as_.get('avg_goals'))} | "
        f"GA {fmt(as_.get('avg_gc'))} | "
        f"xG {fmt(as_.get('avg_xg'))} | "
        f"Shots {fmt(as_.get('avg_shots_for'))} | "
        f"SoT {fmt(as_.get('avg_sot_for'))}",
        f"{home} (home)   G {fmt(hs.get('avg_goals'))} | "
        f"GA {fmt(hs.get('avg_gc'))} | "
        f"xG {fmt(hs.get('avg_xg'))} | "
        f"Shots {fmt(hs.get('avg_shots_for'))} | "
        f"SoT {fmt(hs.get('avg_sot_for'))}",
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
        f"🚀 Job STARTED (soccer weak-away-team alert)\n"
        f"Batch START={START} LIMIT={LIMIT}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting 365scores weak-away-team alert script...")
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
                f"⚠️ Weak-away-team alert FINISHED (No matches)\n"
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

                    weak_away_msg = evaluate_weak_away_signal(
                        home,
                        away,
                        home_data,
                        away_data,
                        m_url
                    )

                    if weak_away_msg:
                        log.info("ALERT (weak away team):\n" + weak_away_msg)
                        scraper.send_telegram_message(
                            weak_away_msg,
                            BOT_TOKEN,
                            CHAT_ID
                        )
                    else:
                        log.info("No weak-away-team signal found.")

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
                f"✅ Weak-away-team alert FINISHED\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, analyzed "
                f"{analyzed_count}/{len(batch_matches)} in this batch",
                BOT_TOKEN,
                CHAT_ID
            )

    except Exception as e:

        log.error(f"Weak-away-team alert job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Weak-away-team alert FAILED\n"
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
