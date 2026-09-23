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


# ---------------- POSSESSION DOMINANCE SIGNAL ENGINE ----------------
#
# The previous "match winner for every match" predictor has been
# removed. This is back to a gated filter, focused on one specific
# claim: one team both DOMINATES the ball AND turns that dominance
# into real chance creation against this specific opponent — not just
# sterile, side-to-side possession with nothing to show for it.
# That's deliberately two separate requirements, not one: possession
# share alone says nothing about end product (a team can have 65% of
# the ball and create less than a team happy to sit deep and counter),
# so the hard gate requires BOTH a clear possession gap AND that team
# actually out-creating the opponent on shots, shots on target, and
# big chances.
#
# NOTE ON CONFIDENCE: same disclaimer as every version of this file —
# every threshold below is a heuristic cutoff, not a measured
# probability.

MIN_SAMPLE_MATCHES = TEAM_SAMPLE_MATCHES

# What counts as "dominates possession": a high own-average floor,
# AND a wide gap over the opponent's own average — a team on 56%
# against an opponent on 54% isn't "dominant", it's a coin flip.
POSSESSION_DOMINANT_MIN = 56.0
POSSESSION_GAP_MIN = 10.0

# How much of an xG edge on top of the raw shot-count edge counts as
# "the extra chances are genuinely better quality, not just more of
# the same speculative efforts".
XG_QUALITY_EDGE_MIN = 0.5

# Corroboration bar on top of the hard gate (possession dominance +
# out-creating the opponent on shots/SoT/big chances). Max achievable
# is 5.0 (1 xG quality edge + 1 more corners/territory + 1 own defense
# solid enough that dominating the ball also means limiting counters +
# 1 opponent genuinely starved for shots + 1 healthy volume of big
# chances, not just more than a weak opponent); set at 60% of that.
POSSESSION_SCORE_THRESHOLD = 3.0

# "Genuinely starved" / "healthy volume" absolute bars, used alongside
# the relative comparisons in the hard gate — an opponent having fewer
# shots than the dominant side still matters less if they're both
# generating plenty; these check the actual numbers, not just who's
# ahead.
OPPONENT_STARVED_MAX_SHOTS = 9.0
HEALTHY_BIG_CHANCES_MIN = 1.8


def _escape_markdown(text):
    """
    Minimal escaping for Telegram's legacy "Markdown" parse mode: only
    _, *, ` and [ need escaping there.
    """
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def _dominates_possession_and_chances(team, opp):
    """
    Hard gate: True only if `team` both dominates the ball against
    `opp` (POSSESSION_DOMINANT_MIN / POSSESSION_GAP_MIN) AND converts
    that into more shots, more shots on target, and more big chances
    than `opp` — all three, no partial credit, since the whole point
    of this filter is possession that actually produces something.
    xG is deliberately NOT part of this hard gate (folded into the
    corroboration score instead) so a minor-league match missing xG
    data can still qualify on the raw counting stats. Fails closed on
    any missing input.
    """
    team_poss, opp_poss = team.get("avg_possession"), opp.get("avg_possession")
    if team_poss is None or opp_poss is None:
        return False

    if team_poss < POSSESSION_DOMINANT_MIN:
        return False

    if team_poss - opp_poss < POSSESSION_GAP_MIN:
        return False

    team_shots, opp_shots = team.get("avg_shots_for"), opp.get("avg_shots_for")
    team_sot, opp_sot = team.get("avg_sot_for"), opp.get("avg_sot_for")
    team_bc, opp_bc = team.get("avg_big_chances_for"), opp.get("avg_big_chances_for")

    if None in (team_shots, opp_shots, team_sot, opp_sot, team_bc, opp_bc):
        return False

    return team_shots > opp_shots and team_sot > opp_sot and team_bc > opp_bc


def _possession_dominance_score(team, opp):
    """
    Corroboration score on top of the hard gate — every factor here is
    an extra, independent reason the possession dominance is
    genuinely meaningful rather than just "more touches, marginally
    more shots". All lookups None-safe.
    """
    score = 0.0

    team_xg, opp_xg = team.get("avg_xg"), opp.get("avg_xg")
    if team_xg is not None and opp_xg is not None and team_xg - opp_xg >= XG_QUALITY_EDGE_MIN:
        score += 1

    team_corners, opp_corners = team.get("avg_corners_for"), opp.get("avg_corners_for")
    if team_corners is not None and opp_corners is not None and team_corners > opp_corners:
        score += 1

    team_gc = team.get("avg_gc", 0)
    if team_gc <= 1.0:
        score += 1

    opp_shots = opp.get("avg_shots_for")
    if opp_shots is not None and opp_shots <= OPPONENT_STARVED_MAX_SHOTS:
        score += 1

    team_bc = team.get("avg_big_chances_for")
    if team_bc is not None and team_bc >= HEALTHY_BIG_CHANCES_MIN:
        score += 1

    return score


def evaluate_possession_dominance_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if either side both dominates
    possession against the other AND out-creates them on shots/SoT/big
    chances (see _dominates_possession_and_chances), and clears
    POSSESSION_SCORE_THRESHOLD worth of corroboration (see
    _possession_dominance_score) — or None if neither direction clears
    both bars. Single bidirectional function via the `dominance_case`
    closure, same pattern this file's earlier signals used.
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

    def dominance_case(team_stats, opp_stats):
        if not _dominates_possession_and_chances(team_stats, opp_stats):
            return None

        score = _possession_dominance_score(team_stats, opp_stats)

        if score < POSSESSION_SCORE_THRESHOLD:
            return None

        return score

    result = None
    team_stats, opp_stats, team_name, opp_name = None, None, None, None

    home_score = dominance_case(hs, as_)
    if home_score is not None:
        result = home_score
        team_stats, opp_stats, team_name, opp_name = hs, as_, home, away
    else:
        away_score = dominance_case(as_, hs)
        if away_score is not None:
            result = away_score
            team_stats, opp_stats, team_name, opp_name = as_, hs, away, home

    if result is None:
        return None

    score = result

    # -------------------------------------------------
    # RISK FACTORS (shown, don't block the prediction)
    # -------------------------------------------------

    risks = []

    team_xg = team_stats.get("avg_xg")
    team_g = team_stats.get("avg_goals", 0)
    if team_xg is not None and team_g <= team_xg - 0.3:
        risks.append(
            f"{team_name} has been under-converting its own chance "
            f"quality (goals {team_g} vs xG {team_xg}) — the "
            f"dominance is real, but it isn't always turning into "
            f"goals"
        )

    team_shots_against = team_stats.get("avg_shots_against")
    team_gc = team_stats.get("avg_gc", 0)
    if team_shots_against is not None and team_shots_against >= 10.0:
        risks.append(
            f"{team_name} still concedes {team_shots_against} "
            f"shots/match despite dominating the ball — some "
            f"counter-attack exposure even while in control"
        )

    opp_xg = opp_stats.get("avg_xg")
    if opp_xg is not None and opp_xg >= 1.0:
        risks.append(
            f"{opp_name} still averages {opp_xg} xG/match even with "
            f"the ball taken off them — capable of making the few "
            f"chances they get count"
        )

    # -------------------------------------------------
    # MESSAGE
    # -------------------------------------------------

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"🔵 *{home} vs {away}*",
        "",
        f"🎯 *Prediction: {team_name} to dominate possession and "
        f"control chance creation against {opp_name}*",
        f"Possession {fmt(team_stats.get('avg_possession'))}% vs "
        f"{fmt(opp_stats.get('avg_possession'))}% | corroboration "
        f"score {score:.1f} (bar: {POSSESSION_SCORE_THRESHOLD:.1f})",
        "",
        "📊 *Stats*",
        f"{team_name}   Poss {fmt(team_stats.get('avg_possession'))}% | "
        f"Shots {fmt(team_stats.get('avg_shots_for'))} | "
        f"SoT {fmt(team_stats.get('avg_sot_for'))} | "
        f"BigCh {fmt(team_stats.get('avg_big_chances_for'))} | "
        f"xG {fmt(team_stats.get('avg_xg'))}",
        f"{opp_name}   Poss {fmt(opp_stats.get('avg_possession'))}% | "
        f"Shots {fmt(opp_stats.get('avg_shots_for'))} | "
        f"SoT {fmt(opp_stats.get('avg_sot_for'))} | "
        f"BigCh {fmt(opp_stats.get('avg_big_chances_for'))} | "
        f"xG {fmt(opp_stats.get('avg_xg'))}",
        f"Corners {fmt(team_stats.get('avg_corners_for'))} vs "
        f"{fmt(opp_stats.get('avg_corners_for'))} | "
        f"GA {fmt(team_stats.get('avg_gc'))} vs "
        f"{fmt(opp_stats.get('avg_gc'))} | "
        f"Shots against {fmt(team_stats.get('avg_shots_against'))} vs "
        f"{fmt(opp_stats.get('avg_shots_against'))}",
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
        f"🚀 Job STARTED (soccer possession-dominance alert)\n"
        f"Batch START={START} LIMIT={LIMIT}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting 365scores possession-dominance alert script...")
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
                f"⚠️ Possession-dominance alert FINISHED (No matches)\n"
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

                    dominance_msg = evaluate_possession_dominance_signal(
                        home,
                        away,
                        home_data,
                        away_data,
                        m_url
                    )

                    if dominance_msg:
                        log.info("ALERT (possession dominance):\n" + dominance_msg)
                        scraper.send_telegram_message(
                            dominance_msg,
                            BOT_TOKEN,
                            CHAT_ID
                        )
                    else:
                        log.info("No possession-dominance signal found.")

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
                f"✅ Possession-dominance alert FINISHED\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, analyzed "
                f"{analyzed_count}/{len(batch_matches)} in this batch",
                BOT_TOKEN,
                CHAT_ID
            )

    except Exception as e:

        log.error(f"Possession-dominance alert job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Possession-dominance alert FAILED\n"
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
