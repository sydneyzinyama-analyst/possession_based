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
LOG_PATH = os.getenv("SCRAPER_LOG_PATH", "tennis_match_stats_alert.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tennis_match_stats_alert")


# ---------------- TUNABLES ----------------
# 365scores.com JSON API (webws.365scores.com/web/...) — the same API
# the football script uses, with sports=3 for tennis.
TENNIS_SPORT_ID = 3

REQUEST_TIMEOUT_SEC = int(os.getenv("SCRAPER_REQUEST_TIMEOUT_SEC", "20"))
MAX_RETRIES = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))
RETRY_BACKOFF_SEC = float(os.getenv("SCRAPER_RETRY_BACKOFF_SEC", "1.5"))

# How many of each player's own recent finished matches to analyze.
PLAYER_SAMPLE_MATCHES = 6

# How many of those recent results to list individually in the message.
RECENT_RESULTS_SHOWN = 5

# Telegram allows roughly one message per second per chat; since this
# script sends one message for every match, pace the sends.
TELEGRAM_SEND_INTERVAL_SEC = float(os.getenv("TELEGRAM_SEND_INTERVAL_SEC", "1.1"))


# ---------------- JOB STATUS TELEGRAM ----------------
def send_job_status(message, bot_token, chat_id):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        requests.post(url, data=payload, timeout=20)
    except Exception as e:
        log.warning(f"Failed to send job status to Telegram: {e}")


# ---------------- SCRAPER CLASS ----------------
class TennisScraper:
    """
    Talks directly to 365scores.com's own internal JSON API
    (webws.365scores.com/web/...) with plain requests calls. A
    "player" here is a 365scores competitor, which for doubles is the
    pair (e.g. "King E./Stevens B.").
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
                "sports": TENNIS_SPORT_ID,
                "startDate": date_str,
                "endDate": date_str,
                "showOdds": "true",
                "onlyMajorGames": "false",
                "withTop": "true",
            },
        )

        matches = []
        skipped_not_upcoming = 0

        competition_slugs = {
            c.get("id"): c.get("nameForURL")
            for c in (data or {}).get("competitions") or []
        }

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
                    "url": self._match_url(
                        g, home, away,
                        competition_slugs.get(g.get("competitionId")),
                    ),
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

    @staticmethod
    def _match_url(g, home, away, competition_slug):
        """
        365scores' own match page link:
        /tennis/match/<comp>-<compId>/<home>-<away>-<homeId>-<awayId>-<compId>#id=<gameId>
        (a bare /tennis/game/<gameId> link redirects to the league page).
        """
        comp_id = g.get("competitionId")
        home_slug = home.get("nameForURL")
        away_slug = away.get("nameForURL")
        if not (competition_slug and comp_id and home_slug and away_slug):
            return f"https://www.365scores.com/en-uk/tennis#id={g['id']}"
        return (
            f"https://www.365scores.com/en-uk/tennis/match/"
            f"{competition_slug}-{comp_id}/"
            f"{home_slug}-{away_slug}-{home['id']}-{away['id']}-{comp_id}"
            f"#id={g['id']}"
        )

    # ---------------- PLAYER HISTORY ----------------
    def get_player_recent_matches(self, player_id, count=5):
        """
        The player's most recent finished matches, newest first. Skips
        cancelled matches and walkovers (no sets played, scores are -1).
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
            home_sets = home.get("score")
            away_sets = away.get("score")

            if home.get("id") is None or away.get("id") is None:
                continue
            if home_sets is None or away_sets is None:
                continue
            if home_sets < 0 or away_sets < 0:
                continue

            # isWinner also covers retirements, where the set score
            # alone can be level or even favour the retiring player.
            if home.get("isWinner"):
                winner_id = home["id"]
            elif away.get("isWinner"):
                winner_id = away["id"]
            elif home_sets != away_sets:
                winner_id = home["id"] if home_sets > away_sets else away["id"]
            else:
                continue

            results.append({
                "id": g["id"],
                "home_id": home["id"],
                "home_name": home.get("name", ""),
                "away_id": away["id"],
                "away_name": away.get("name", ""),
                "home_sets": int(home_sets),
                "away_sets": int(away_sets),
                "winner_id": winner_id,
                "tournament": g.get("competitionDisplayName", ""),
            })

            if len(results) >= count:
                break

        return results

    # ---------------- MATCH STATISTICS ----------------
    # 365scores tennis stat name -> our key. "ratio" stats come back as
    # "won/total (pct%)" and are aggregated by summing won and total
    # across matches (so a 3/4 match doesn't weigh the same as 30/40);
    # "count" stats are plain numbers and are averaged per match.
    STAT_NAME_MAP = {
        "Aces": ("aces", "count"),
        "Double Faults": ("double_faults", "count"),
        "1st Serve Points Won": ("first_serve_won", "ratio"),
        "2nd Serve Points Won": ("second_serve_won", "ratio"),
        "Service Points": ("service_points_won", "ratio"),
        # Service Games is "own holds / holds by BOTH players", not
        # "own holds / own service games" — see analyze_player().
        "Service Games": ("service_games_won", "ratio"),
        "Break Points Won": ("break_points_won", "ratio"),
        "2nd Return Points Won": ("second_return_won", "ratio"),
        "Total Points Won": ("total_points_won", "ratio"),
        "Total Games Won": ("total_games_won", "ratio"),
    }

    RATIO_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)")

    def _empty_stat_result(self):
        result = {}
        for stat_key, _ in self.STAT_NAME_MAP.values():
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
                mapped = self.STAT_NAME_MAP.get(item.get("name"))
                if not mapped:
                    continue
                stat_name, kind = mapped

                value = self._parse_stat_value(item.get("value"), kind)
                if value is None:
                    continue

                competitor_id = item.get("competitorId")
                if competitor_id == home_id:
                    result[f"home_{stat_name}"] = value
                elif competitor_id == away_id:
                    result[f"away_{stat_name}"] = value
        except Exception as e:
            log.warning(
                f"Error parsing statistics for match {match_id}: {e}"
            )

        return result

    def _parse_stat_value(self, raw_value, kind):
        """
        "count" -> float, "ratio" -> (won, total) tuple. None if the
        value can't be parsed.
        """
        if raw_value is None:
            return None

        if kind == "ratio":
            m = self.RATIO_RE.match(str(raw_value))
            if not m:
                return None
            return (int(m.group(1)), int(m.group(2)))

        try:
            return float(str(raw_value).rstrip("%"))
        except (TypeError, ValueError):
            return None

    # ---------------- STAT AGGREGATION ----------------
    def _side_values(self, results, stat_name, player_id, side="for"):
        """
        The stat's value in each match, from the player's own side
        ("for") or the opponent's side ("against"), skipping matches
        without it.
        """
        values = []
        for r in results:
            is_home = r.get("home_id") == player_id
            is_away = r.get("away_id") == player_id
            if not is_home and not is_away:
                continue

            own, opp = ("home", "away") if is_home else ("away", "home")
            value = r.get(f"{own if side == 'for' else opp}_{stat_name}")
            if value is not None:
                values.append(value)
        return values

    def _count_avg(self, results, stat_name, player_id, side="for"):
        values = self._side_values(results, stat_name, player_id, side)
        if not values:
            return None
        return round(sum(values) / len(values), 1)

    def _ratio_totals(self, results, stat_name, player_id, side="for"):
        values = self._side_values(results, stat_name, player_id, side)
        if not values:
            return None
        won = sum(v[0] for v in values)
        total = sum(v[1] for v in values)
        return (won, total) if total > 0 else None

    @staticmethod
    def _pct(won, total):
        if total is None or total <= 0 or won is None:
            return None
        return round(100 * won / total)

    def _ratio_pct(self, results, stat_name, player_id, side="for"):
        totals = self._ratio_totals(results, stat_name, player_id, side)
        return self._pct(*totals) if totals else None

    def _recent_results(self, results, player_id):
        """
        One entry per recent match from the player's point of view,
        newest first.
        """
        out = []
        for r in results:
            if r.get("home_id") == player_id:
                sets_for, sets_against = r["home_sets"], r["away_sets"]
                opponent = r["away_name"]
            elif r.get("away_id") == player_id:
                sets_for, sets_against = r["away_sets"], r["home_sets"]
                opponent = r["home_name"]
            else:
                continue
            out.append({
                "won": r["winner_id"] == player_id,
                "sets_for": sets_for,
                "sets_against": sets_against,
                "opponent": opponent,
                "tournament": r.get("tournament", ""),
            })
        return out

    # ---------------- PLAYER ANALYSIS ----------------
    def analyze_player(self, player_id, player_name=None):
        t0 = time.time()
        player_name = player_name or str(player_id)

        recent = self.get_player_recent_matches(
            player_id, count=PLAYER_SAMPLE_MATCHES
        )
        results = []
        for m in recent:
            match_stats = self.get_match_statistics(
                m["id"], m["home_id"], m["away_id"]
            )
            match_data = dict(m)
            match_data.update(match_stats)
            results.append(match_data)

        log.info(
            f"analyze_player({player_name!r}): {len(results)}/"
            f"{PLAYER_SAMPLE_MATCHES} matches fetched in "
            f"{time.time()-t0:.1f}s total"
        )

        recent_results = self._recent_results(results, player_id)
        wins = sum(1 for r in recent_results if r["won"])

        pct = lambda stat, side="for": self._ratio_pct(results, stat, player_id, side)
        avg = lambda stat, side="for": self._count_avg(results, stat, player_id, side)

        # First serves in: 1st-serve points played out of all service
        # points played.
        first = self._ratio_totals(results, "first_serve_won", player_id)
        serve = self._ratio_totals(results, "service_points_won", player_id)
        first_serve_in = (
            self._pct(first[1], serve[1]) if first and serve else None
        )

        # Return points won: every point won that wasn't won on serve,
        # out of every point played that wasn't on serve.
        total = self._ratio_totals(results, "total_points_won", player_id)
        return_points_won = (
            self._pct(total[0] - serve[0], total[1] - serve[1])
            if total and serve
            else None
        )

        # Break points saved: the opponent's break points NOT converted.
        opp_bp = self._ratio_totals(
            results, "break_points_won", player_id, "against"
        )
        bp_saved = (
            self._pct(opp_bp[1] - opp_bp[0], opp_bp[1]) if opp_bp else None
        )

        # Hold %: own service games won out of own service games
        # played, where every own service game not held was a break
        # by the opponent. Only matches with both numbers count.
        holds = breaks_against = 0
        for r in results:
            own, opp = (
                ("home", "away") if r.get("home_id") == player_id
                else ("away", "home")
            )
            held = r.get(f"{own}_service_games_won")
            broken = r.get(f"{opp}_break_points_won")
            if held is None or broken is None:
                continue
            holds += held[0]
            breaks_against += broken[0]
        hold_pct = self._pct(holds, holds + breaks_against)

        return {
            "player": player_name,
            "player_id": player_id,
            "matches": len(results),
            "wins": wins,
            "form": "".join("W" if r["won"] else "L" for r in recent_results),
            "recent_results": recent_results,
            "avg_aces": avg("aces"),
            "avg_double_faults": avg("double_faults"),
            "first_serve_in_pct": first_serve_in,
            "first_serve_won_pct": pct("first_serve_won"),
            "second_serve_won_pct": pct("second_serve_won"),
            "service_points_won_pct": pct("service_points_won"),
            "hold_pct": hold_pct,
            "bp_saved_pct": bp_saved,
            "return_points_won_pct": return_points_won,
            "second_return_won_pct": pct("second_return_won"),
            "bp_converted_pct": pct("break_points_won"),
            "total_points_won_pct": pct("total_points_won"),
            "total_games_won_pct": pct("total_games_won"),
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


def _player_block(label, s):
    """
    Stats section for one player. `s` is analyze_player()'s return
    value, or None if that player's analysis failed.
    """
    if s is None:
        return [f"*{label}*", "   (no data — analysis failed)"]
    if s["matches"] == 0:
        return [f"*{label}*", "   (no recent finished matches found)"]

    lines = [
        f"*{label}*  (won {s['wins']}/{s['matches']}, form {s['form'] or 'N/A'})",
        f"   Aces {_fmt(s['avg_aces'])} | Double faults {_fmt(s['avg_double_faults'])}",
        f"   1st serve in {_fmt(s['first_serve_in_pct'], '%')} | "
        f"1st won {_fmt(s['first_serve_won_pct'], '%')} | "
        f"2nd won {_fmt(s['second_serve_won_pct'], '%')}",
        f"   Service pts won {_fmt(s['service_points_won_pct'], '%')} | "
        f"Holds {_fmt(s['hold_pct'], '%')} | "
        f"BP saved {_fmt(s['bp_saved_pct'], '%')}",
        f"   Return pts won {_fmt(s['return_points_won_pct'], '%')} | "
        f"2nd return won {_fmt(s['second_return_won_pct'], '%')} | "
        f"BP converted {_fmt(s['bp_converted_pct'], '%')}",
        f"   Total pts won {_fmt(s['total_points_won_pct'], '%')} | "
        f"Games won {_fmt(s['total_games_won_pct'], '%')}",
    ]

    shown = s["recent_results"][:RECENT_RESULTS_SHOWN]
    if shown:
        lines.append("   Recent:")
        for r in shown:
            lines.append(
                f"   {'✅' if r['won'] else '❌'} {r['sets_for']}-{r['sets_against']} "
                f"vs {_escape_markdown(r['opponent'])} "
                f"({_escape_markdown(r['tournament'])})"
            )

    return lines


def build_match_message(match, home_data, away_data, m_url):
    home = _escape_markdown(match["home_name"])
    away = _escape_markdown(match["away_name"])
    tournament = _escape_markdown(match.get("tournament") or "")
    kickoff = match.get("start_time") or ""

    lines = [f"🎾 *{home} vs {away}*"]
    if tournament:
        lines.append(f"🏆 {tournament}")
    if kickoff:
        # startTime looks like 2026-09-28T13:20:00+02:00
        lines.append(f"🕒 {kickoff[:16].replace('T', ' ')}")
    lines.append("")
    lines.append(f"📈 *Last {PLAYER_SAMPLE_MATCHES} matches*")
    lines.extend(_player_block(home, home_data))
    lines.append("")
    lines.extend(_player_block(away, away_data))
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
        f"🚀 Job STARTED (tennis match stats alert)\n"
        f"Batch START={START} LIMIT={LIMIT}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting 365scores tennis match stats alert script...")
    log.info(f"Batch start={START}, limit={LIMIT}")

    scraper = None
    matches = []
    sent_count = 0

    try:
        scraper = TennisScraper()

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
                f"⚠️ Tennis match stats alert FINISHED (No matches)\n"
                f"Batch START={START} LIMIT={LIMIT}\n"
                f"Found {len(matches)} matches today, 0 fell in this "
                f"batch's range",
                BOT_TOKEN,
                CHAT_ID
            )
            return

        for idx, match in enumerate(batch_matches, start=START + 1):
            m_url = match["url"]
            home = match["home_name"]
            away = match["away_name"]

            log.info(
                f"Processing match {idx}: {home} vs {away} "
                f"({match.get('tournament', '')}) {m_url}"
            )

            try:
                if not home or not away:
                    log.warning("Could not extract players, skipping match")
                    continue

                # A failed side still gets sent, shown as "no data".
                try:
                    home_data = scraper.analyze_player(match["home_id"], home)
                except Exception as e:
                    log.error(f"Home player analysis failed: {e}")
                    home_data = None

                try:
                    away_data = scraper.analyze_player(match["away_id"], away)
                except Exception as e:
                    log.error(f"Away player analysis failed: {e}")
                    away_data = None

                msg = build_match_message(match, home_data, away_data, m_url)
                log.info("MATCH STATS:\n" + msg)
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
            f"✅ Tennis match stats alert FINISHED\n"
            f"Batch START={START} LIMIT={LIMIT}\n"
            f"Found {len(matches)} matches today, sent "
            f"{sent_count}/{len(batch_matches)} in this batch",
            BOT_TOKEN,
            CHAT_ID
        )

    except Exception as e:
        log.error(f"Tennis match stats alert job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Tennis match stats alert FAILED\n"
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
