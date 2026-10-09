import os
import sys
import argparse
import logging
import math
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

# ---------------- MATCH UNDER 3.5 GOALS SIGNAL ----------------
# Deterministic math, not a judgment call: both teams' expected goals
# (see _expected_goals) are summed into one match-total rate, which
# feeds a Poisson model's P(total <= 3) — the Poisson CDF at k=3 is
# exactly P(Under 3.5), since 3.5 sits between the integers 3 and 4
# with no push case to worry about.
#
# NOTE: a calculated 90% doesn't guarantee it's *right* 90% of the
# time, only that the Poisson math was done correctly on the inputs it
# was given — this file's history already has two prior filters that
# looked reasonable and were swapped out (an Over/Under Poisson model
# across several lines, then a team-to-score-1+ signal, then a
# strong-attack-vs-weak-defense matchup filter before that); this one
# needs the same real-result check (match_stats_backtest.py) before
# the ALERT_PROBABILITY_THRESHOLD bar can be trusted as-is.

MIN_SAMPLE_MATCHES = TEAM_SAMPLE_MATCHES

# Only alert when the match's P(Under 3.5) clears this bar. 0.90 = 90%,
# per explicit request. Env-overridable, matching this file's existing
# tunable style.
ALERT_PROBABILITY_THRESHOLD = float(os.getenv("ALERT_PROBABILITY_THRESHOLD", "0.90"))

# The Under lines this signal checks, per explicit request. A match
# alerts once if ANY line clears ALERT_PROBABILITY_THRESHOLD, and the
# message lists every line that does. Any match clearing 3.5 also
# clears 4.5, so 4.5 adds matches with a bit more expected goals
# (~2.43 combined vs ~1.74 for 3.5 at the 90% bar).
GOAL_LINES = [3.5, 4.5]


def _expected_goals(home_data, away_data):
    """
    Each side's expected goals for this match: own attacking rate
    blended with the opponent's own defensive leakiness (xG-based when
    both teams have xG data, goals-based fallback otherwise) — the
    same "for + opponent's against" blend flashscore_scraper.py uses
    throughout its signal engine. Returns
    (expected_home_goals, expected_away_goals, basis).
    """
    h_xg, h_xga = home_data.get("avg_xg"), home_data.get("avg_xga")
    a_xg, a_xga = away_data.get("avg_xg"), away_data.get("avg_xga")

    if None not in (h_xg, h_xga, a_xg, a_xga):
        return (h_xg + a_xga) / 2, (a_xg + h_xga) / 2, "xG-based"

    h_g, h_gc = home_data.get("avg_goals") or 0, home_data.get("avg_gc") or 0
    a_g, a_gc = away_data.get("avg_goals") or 0, away_data.get("avg_gc") or 0
    return (h_g + a_gc) / 2, (a_g + h_gc) / 2, "goals-based, no xG data"


def _poisson_pmf(k, lam):
    """P(exactly k) for a Poisson(lam) variable. Pure math.exp/factorial — no numpy needed."""
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _poisson_cdf(k, lam):
    """P(X <= k) for a Poisson(lam) variable, by summing the PMF up to k."""
    return sum(_poisson_pmf(i, lam) for i in range(k + 1))


def _prob_under(expected_total, line):
    """P(Under `line`) for a Poisson(expected_total) match-total — line is always X.5, so this is exact (no push)."""
    return _poisson_cdf(int(line), expected_total)


def evaluate_under_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if this match's combined-goals
    Poisson model puts P(Under line) at or above
    ALERT_PROBABILITY_THRESHOLD for any line in GOAL_LINES, or None if
    none do (or either team's sample is too thin to trust).
    """
    if (
        (home_data or {}).get("matches", 0) < MIN_SAMPLE_MATCHES
        or (away_data or {}).get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    expected_home, expected_away, basis = _expected_goals(home_data, away_data)
    expected_total = expected_home + expected_away
    passing = [
        (line, p)
        for line in GOAL_LINES
        for p in [_prob_under(expected_total, line)]
        if p >= ALERT_PROBABILITY_THRESHOLD
    ]

    if not passing:
        return None

    home_esc = _escape_markdown(home)
    away_esc = _escape_markdown(away)

    lines = [
        f"🎯 *{home_esc} vs {away_esc}*",
        f"Goals model ({basis}): expected {expected_home:.2f} + "
        f"{expected_away:.2f} = {expected_total:.2f} total",
        "",
    ]
    lines += [
        f"📊 *Under {line} goals — {p*100:.1f}% probability*"
        for line, p in passing
    ]
    lines += [
        "",
        f"🔗 {m_url}",
    ]

    return "\n".join(lines)


# ---------------- STRONG ATTACK vs WEAK DEFENSE SIGNAL ----------------
# Alerts when one side's own attacking record is genuinely strong AND
# the side they're facing has a genuinely leaky defensive record — a
# flagged matchup mismatch, not a predicted scoreline or probability.
# Uses each side's OWN rate independently (not blended with the
# opponent's, unlike the Under signal above) — "strong attack" and
# "weak defense" are properties of a team's own record, not something
# that depends on who they're facing.
#
# xG/xGA preferred over raw goals/conceded when available — steadier
# match-to-match than actual goals, which swings more on finishing
# variance, same xG-preferred pattern flashscore_scraper.py uses.
#
# NOTE: these thresholds are a reasonable starting point (a team
# netting ~2/game is a clear attacking threat; a team shipping ~1.5+/
# game has a real defensive problem), but — same lesson as every
# other filter built in this file's history — they haven't been
# checked against real results yet. Worth backtesting
# (match_stats_backtest.py) before trusting them as-is.

STRONG_ATTACK_GOALS = 2.0
WEAK_DEFENSE_GOALS = 1.5

# Corroboration bar: how many of the extra attack/defense indicators
# (shots, shots on target, big chances — attacker's own rate for vs
# defender's own rate against) have to agree before this fires, out
# of 3 possible. Guards against a mismatch built on a couple of fluky
# high-scoring/leaky games rather than a genuine, repeated pattern.
MISMATCH_SCORE_THRESHOLD = 2


def _is_strong_attack(team_data):
    """True if team_data's own scoring rate (xG preferred over goals) clears STRONG_ATTACK_GOALS."""
    rate = team_data.get("avg_xg")
    if rate is None:
        rate = team_data.get("avg_goals")
    return rate is not None and rate >= STRONG_ATTACK_GOALS


def _is_weak_defense(team_data):
    """True if team_data's own conceding rate (xGA preferred over goals conceded) clears WEAK_DEFENSE_GOALS."""
    rate = team_data.get("avg_xga")
    if rate is None:
        rate = team_data.get("avg_gc")
    return rate is not None and rate >= WEAK_DEFENSE_GOALS


def _mismatch_score(attacker_data, defender_data):
    """
    Corroboration on top of the hard gate: for each of shots/shots-on-
    target/big-chances, does the attacker create a lot of them AND
    does the defender concede a lot of them — 0 to 3. None-safe; a
    missing stat contributes nothing.
    """
    score = 0
    # (attacker's own "for" stat, defender's own "against" stat, bar both must clear)
    checks = (
        ("avg_shots_for", "avg_shots_against", 12.0),
        ("avg_sot_for", "avg_sot_against", 4.5),
        ("avg_big_chances_for", "avg_big_chances_against", 2.0),
    )
    for for_key, against_key, bar in checks:
        attacker_rate = attacker_data.get(for_key)
        defender_rate = defender_data.get(against_key)
        if attacker_rate is not None and defender_rate is not None:
            if attacker_rate >= bar and defender_rate >= bar:
                score += 1
    return score


def evaluate_attack_vs_defense_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if either side's strong attacking
    record is facing the other side's weak defensive record,
    corroborated by at least MISMATCH_SCORE_THRESHOLD of the shots/
    SoT/big-chances indicators — or None if neither direction
    qualifies, or either team's sample is too thin to trust. Checks
    both directions independently; a match can fire for one side,
    both, or neither.
    """
    if (
        (home_data or {}).get("matches", 0) < MIN_SAMPLE_MATCHES
        or (away_data or {}).get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    findings = []

    if _is_strong_attack(home_data) and _is_weak_defense(away_data):
        score = _mismatch_score(home_data, away_data)
        if score >= MISMATCH_SCORE_THRESHOLD:
            findings.append(("home", score))

    if _is_strong_attack(away_data) and _is_weak_defense(home_data):
        score = _mismatch_score(away_data, home_data)
        if score >= MISMATCH_SCORE_THRESHOLD:
            findings.append(("away", score))

    if not findings:
        return None

    home_esc = _escape_markdown(home)
    away_esc = _escape_markdown(away)

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [f"⚔️ *{home_esc} vs {away_esc}*", ""]

    for side, score in findings:
        attacker_data = home_data if side == "home" else away_data
        defender_data = away_data if side == "home" else home_data
        attacker_name = home_esc if side == "home" else away_esc
        defender_name = away_esc if side == "home" else home_esc

        attack_rate = attacker_data.get("avg_xg")
        if attack_rate is None:
            attack_rate = attacker_data.get("avg_goals")
        defense_rate = defender_data.get("avg_xga")
        if defense_rate is None:
            defense_rate = defender_data.get("avg_gc")

        lines.append(
            f"*{attacker_name}'s attack vs {defender_name}'s defense* "
            f"(mismatch score {score}/3)"
        )
        lines.append(
            f"   {attacker_name} scoring {fmt(attack_rate)}/game vs "
            f"{defender_name} conceding {fmt(defense_rate)}/game"
        )
        lines.append(
            f"   Shots {fmt(attacker_data.get('avg_shots_for'))} for vs "
            f"{fmt(defender_data.get('avg_shots_against'))} allowed | "
            f"On target {fmt(attacker_data.get('avg_sot_for'))} vs "
            f"{fmt(defender_data.get('avg_sot_against'))} | "
            f"Big chances {fmt(attacker_data.get('avg_big_chances_for'))} vs "
            f"{fmt(defender_data.get('avg_big_chances_against'))}"
        )
        lines.append("")

    lines.append(f"🔗 {m_url}")

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
        f"🚀 Job STARTED (soccer match stats alert)\n"
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
            log.info("No matches in this batch.")
            send_job_status(
                f"⚠️ Match stats alert FINISHED (No matches)\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, 0 fell in this "
                f"batch's range",
                BOT_TOKEN,
                CHAT_ID
            )
            return

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

                # Independent signals — either, both, or neither can
                # fire for a given match; each gets its own alert.
                under_msg = evaluate_under_signal(home, away, home_data, away_data, m_url)
                avd_msg = evaluate_attack_vs_defense_signal(home, away, home_data, away_data, m_url)

                fired_signals = [
                    ("under 3.5/4.5", under_msg),
                    ("attack vs defense", avd_msg),
                ]

                any_fired = False
                for label, sig_msg in fired_signals:
                    if not sig_msg:
                        continue
                    any_fired = True
                    msg = sig_msg + "\n\n" + build_match_message(match, home_data, away_data, m_url)
                    log.info(f"MATCH STATS ({label}):\n" + msg)
                    scraper.send_telegram_message(msg, BOT_TOKEN, CHAT_ID)
                    sent_count += 1

                if not any_fired:
                    log.info("No signals — not alerting.")
                    continue

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


if __name__ == "__main__":
    main()
