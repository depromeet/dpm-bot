"""디프만 19기 활동 포인트 집계 스크립트.

GitHub Actions에서 20분마다 실행된다. 지정한 채널의 기록을 처음부터 다시 읽어
점수를 계산하고 패스 현황판 · 리더보드 · 점수-집계 · 패스 뱃지를 갱신한다.
매번 전체를 다시 계산하므로 배점을 바꾸거나 오류가 나도 다시 돌리면 된다.

    python collect.py --dry-run   디스코드에 아무것도 쓰지 않고 결과만 출력
    python collect.py             실제로 반영
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

API = "https://discord.com/api/v10"
KST = ZoneInfo("Asia/Seoul")
DISCORD_EPOCH_MS = 1420070400000
APPROVE, REJECT = "✅", "❌"
USER_MESSAGE_TYPES = (0, 19)  # 일반 메시지, 답장 (시스템 메시지 제외)
PINNED_THREAD = 1 << 1  # 포럼에 고정된 글의 채널 플래그
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")
MISSION_NO = re.compile(r"^\s*#?\s*(\d{1,2})(?!\d)")
USER_TAG = re.compile(r"<@!?(\d+)>")
NO_PINGS = {"parse": []}
MAX_LEN = 1900  # 디스코드 메시지 2,000자 제한에 여유를 둠
SCORE_LABELS = ["질문", "번개참여", "번개주최", "스터디참여", "스터디개설", "패스", "후기"]


# ── 디스코드 API ──────────────────────────────────────────────

class Discord:
    def __init__(self, token: str, dry_run: bool):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/depromeet, 1.0)",
        })
        self.dry_run = dry_run
        self.writes = 0
        self.errors: list[str] = []

    def _request(self, method: str, path: str, **kw):
        for _ in range(5):
            r = self.session.request(method, API + path, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
                continue
            if r.headers.get("X-RateLimit-Remaining") == "0":
                time.sleep(float(r.headers.get("X-RateLimit-Reset-After", 0)))
            if not r.ok:
                raise RuntimeError(f"{method} {path} → {r.status_code} {r.text[:200]}")
            return r.json() if r.content else None
        raise RuntimeError(f"{method} {path} → 요청 한도 초과")

    def get(self, path: str, **params):
        return self._request("GET", path, params=params)

    def write(self, method: str, path: str, body: dict | None = None):
        """쓰기 요청. --dry-run이면 보내지 않고, 실패해도 나머지 작업은 계속한다."""
        if self.dry_run:
            return None
        self.writes += 1
        try:
            return self._request(method, path, json=body)
        except RuntimeError as e:
            self.errors.append(str(e))
            return None

    def members(self, guild_id: str) -> list[dict]:
        out, after = [], "0"
        while True:
            page = self.get(f"/guilds/{guild_id}/members", limit=1000, after=after)
            out += page
            if len(page) < 1000:
                return out
            after = page[-1]["user"]["id"]

    def messages(self, channel_id: str) -> list[dict]:
        """채널(또는 스레드)의 모든 메시지를 오래된 순으로."""
        out, before = [], None
        while True:
            params = {"limit": 100} | ({"before": before} if before else {})
            page = self.get(f"/channels/{channel_id}/messages", **params)
            out += page
            if len(page) < 100:
                return sorted(out, key=lambda m: int(m["id"]))
            before = page[-1]["id"]

    def forum_threads(self, forum_id: str, active: list[dict]) -> list[dict]:
        """포럼의 모든 글(활성 + 보관됨). 활성 목록은 서버 단위로 한 번만 받아서 넘긴다."""
        threads = [t for t in active if t.get("parent_id") == forum_id]
        before = None
        while True:
            params = {"limit": 100} | ({"before": before} if before else {})
            page = self.get(f"/channels/{forum_id}/threads/archived/public", **params)
            threads += page["threads"]
            if not page.get("has_more") or not page["threads"]:
                return threads
            before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]

    def reacted_by(self, channel_id: str, msg: dict, emoji: str, user_ids: set[str]) -> bool:
        """user_ids 중 누군가 이 이모지를 달았는지. 그 이모지가 없으면 API를 부르지 않는다."""
        if not any(r["emoji"]["name"] == emoji for r in msg.get("reactions", [])):
            return False
        users = self.get(f"/channels/{channel_id}/messages/{msg['id']}/reactions/{quote(emoji)}", limit=100)
        return any(u["id"] in user_ids for u in users)

    def sync_messages(self, channel_id: str, bot_id: str, chunks: list[str]) -> None:
        """채널에 봇이 올린 메시지를 chunks와 같아지게 수정·추가·삭제한다."""
        mine = sorted(
            (m for m in self.get(f"/channels/{channel_id}/messages", limit=50) if m["author"]["id"] == bot_id),
            key=lambda m: int(m["id"]),
        )
        for i, text in enumerate(chunks):
            body = {"content": text, "allowed_mentions": NO_PINGS}
            if i >= len(mine):
                self.write("POST", f"/channels/{channel_id}/messages", body)
            elif mine[i]["content"] != text:
                self.write("PATCH", f"/channels/{channel_id}/messages/{mine[i]['id']}", body)
        for m in mine[len(chunks):]:
            self.write("DELETE", f"/channels/{channel_id}/messages/{m['id']}")


# ── 점수 계산 ────────────────────────────────────────────────

def sent_at(snowflake: str) -> datetime:
    """디스코드 ID에 들어 있는 생성 시각."""
    return datetime.fromtimestamp(((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000, KST)


def is_user_message(msg: dict) -> bool:
    return msg.get("type", 0) in USER_MESSAGE_TYPES


def is_pinned(thread: dict) -> bool:
    """포럼 맨 위에 고정된 안내 글. 점수 집계에서 뺀다."""
    return bool(thread.get("flags", 0) & PINNED_THREAD)


@dataclass
class Season:
    start: date
    end: date

    def contains(self, snowflake: str) -> bool:
        return self.start <= sent_at(snowflake).date() <= self.end

    def week(self, snowflake: str) -> int:
        return (sent_at(snowflake).date() - self.start).days // 7


@dataclass
class Score:
    question_weeks: set[int] = field(default_factory=set)
    bungae_join: set[str] = field(default_factory=set)
    bungae_host: set[str] = field(default_factory=set)
    study_join: set[str] = field(default_factory=set)
    study_open: set[str] = field(default_factory=set)
    missions: set[int] = field(default_factory=set)
    blogs: int = 0

    def stage(self, rules: dict) -> int:
        return sum(len(self.missions) >= s["missions"] for s in rules["pass_stages"])

    def parts(self, rules: dict) -> list[int]:
        """SCORE_LABELS 순서의 종목별 점수."""
        def capped(n: int, rule: dict) -> int:
            return min(n * rule["each"], rule["max"])

        return [
            len(self.question_weeks) * rules["question_per_week"],
            capped(len(self.bungae_join), rules["bungae_join"]),
            capped(len(self.bungae_host), rules["bungae_host"]),
            capped(len(self.study_join), rules["study_join"]),
            capped(len(self.study_open), rules["study_open"]),
            sum(s["points"] for s in rules["pass_stages"][: self.stage(rules)]),
            sum(rules["blog"][: self.blogs]),
        ]


def proof_ok(msg: dict, mission: dict, author: str, admins: set[str]) -> bool:
    """미션 인증 형식(멘션 또는 이미지)을 갖췄는지."""
    if mission["proof"] == "image":
        return any(
            (a.get("content_type") or "").startswith("image/") or a["filename"].lower().endswith(IMAGE_EXTS)
            for a in msg.get("attachments", [])
        )
    mentioned = {u["id"] for u in msg.get("mentions", []) if u["id"] != author and not u.get("bot")}
    if mission.get("mention_admin"):
        mentioned &= admins
    return len(mentioned) >= mission.get("min_mentions", 1)


def pass_board(uid: str, score: Score, missions: list[dict], rules: dict) -> str:
    """디퍼별 패스 글 맨 위에 보이는 현황판."""
    stages = rules["pass_stages"]
    done, total, stage = len(score.missions), len(missions), score.stage(rules)
    if stage == len(stages):
        status = f"🏅 **{stage}단계 완주!** ({done} / {total})"
    else:
        left = stages[stage]["missions"] - done
        if stage:
            reached = f"**{stage}단계 달성!**"
        else:
            reached = "**진행 중이에요**" if done else "**아직 시작 전이에요**"
        status = f"{reached} ({done} / {total}) · {stage + 1}단계까지 {left}개 남았어요"

    lines = [f"🎫 <@{uid}> 님의 디프만 패스", status]
    group = None
    for m in missions:
        if m["group"] != group:
            group = m["group"]
            lines += ["", f"**{group}**"]
        lines.append(f"{'✅' if m['num'] in score.missions else '🔲'} {m['num']}. {m['title']}")
    together = [m["num"] for m in missions if m["proof"] == "mention"]
    alone = [m["num"] for m in missions if m["proof"] == "image"]
    lines += [
        "",
        "📌 이 글에 `미션 번호 + 한 줄`을 남겨주세요.",
        f"함께하는 미션({format_nums(together)})은 **멘션**, 혼자 하는 미션({format_nums(alone)})은 **스샷**!",
        "예) `4 오늘 @박디퍼 님이랑 점심 먹었어요!`",
        f"예) `{alone[0] if alone else 10} 완료했어요!` + 📷 스샷을 **같은 메시지에** 첨부",
        "반영까지 몇 시간 걸릴 수 있어요.",
    ]
    return "\n".join(lines)


def format_nums(nums: list[int]) -> str:
    """[1,2,3,5,10,11,12] → '1~3·5·10~12'. 3개 이상 이어지면 ~로 묶는다."""
    groups: list[list[int]] = []
    for n in sorted(nums):
        if groups and n == groups[-1][-1] + 1:
            groups[-1].append(n)
        else:
            groups.append([n])
    return "·".join(f"{g[0]}~{g[-1]}" if len(g) >= 3 else "·".join(map(str, g)) for g in groups)


# ── 출력 서식 ────────────────────────────────────────────────

def display_name(member: dict) -> str:
    user = member["user"]
    return member.get("nick") or user.get("global_name") or user["username"]


def escape(text: str) -> str:
    """이름에 섞인 마크다운 기호가 서식으로 바뀌지 않게."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", text)


def chunk(lines: list[str]) -> list[str]:
    """줄 목록을 디스코드 메시지 길이에 맞게 나눈다."""
    out, cur = [], []

    def flush():
        if cur:
            out.append("\n".join(cur))
            cur.clear()

    for line in lines:
        if len("\n".join(cur + [line])) > MAX_LEN:
            flush()
        cur.append(line)
    flush()
    return out


# ── 실행 ─────────────────────────────────────────────────────

def run(cfg: dict, dc: Discord) -> None:
    gid, ch, roles, rules = cfg["guild_id"], cfg["channels"], cfg["roles"], cfg["points"]
    season = Season(cfg["season"]["start"], cfg["season"]["end"])
    missions = cfg["missions"]
    by_no = {m["num"]: m for m in missions}

    # 사람 구분: 운영진(서버 소유자 포함)은 집계에서 빠지고 ✅/❌ 권한을 가진다
    bot_id = dc.get("/users/@me")["id"]
    owner_id = dc.get(f"/guilds/{gid}")["owner_id"]
    members = dc.members(gid)
    admin_roles = set(roles["admins"])
    admins = {owner_id} | {m["user"]["id"] for m in members if admin_roles & set(m["roles"])}
    dippers = {m["user"]["id"]: m for m in members if not m["user"].get("bot") and m["user"]["id"] not in admins}
    names = {uid: display_name(m) for uid, m in dippers.items()}
    scores = {uid: Score() for uid in dippers}
    active = dc.get(f"/guilds/{gid}/threads/active")["threads"]

    # 오늘의 질문: 질문 글에 디퍼가 답한 주(week)를 모은다
    for th in dc.forum_threads(ch["question_forum"], active):
        if is_pinned(th):
            continue
        for msg in dc.messages(th["id"]):
            uid = msg["author"]["id"]
            if (uid in scores and msg["id"] != th["id"] and is_user_message(msg)
                    and season.contains(msg["id"])
                    and not dc.reacted_by(th["id"], msg, REJECT, admins)):
                scores[uid].question_weeks.add(season.week(msg["id"]))

    # 번개·스터디: 연 사람이 자기 글에서 멘션한 사람 = 참여자. 참여자가 없으면 주최 점수도 없음
    for forum, host_key, join_key in (
        (ch["bungae_forum"], "bungae_host", "bungae_join"),
        (ch["study_forum"], "study_open", "study_join"),
    ):
        for th in dc.forum_threads(forum, active):
            if is_pinned(th) or not season.contains(th["id"]):
                continue
            msgs = dc.messages(th["id"])
            host = th.get("owner_id")
            starter = next((m for m in msgs if m["id"] == th["id"]), None)
            if starter and dc.reacted_by(th["id"], starter, REJECT, admins):
                continue
            joined = {
                u["id"]
                for m in msgs if m["author"]["id"] == host and is_user_message(m)
                for u in m.get("mentions", []) if u["id"] != host and u["id"] in scores
            }
            if not joined:
                continue
            if host in scores:
                getattr(scores[host], host_key).add(th["id"])
            for uid in joined:
                getattr(scores[uid], join_key).add(th["id"])

    # 디프만 패스: 봇이 만든 글만 본다. 글 주인은 현황판 첫 줄의 멘션으로 찾는다
    posts: dict[str, tuple[dict, dict]] = {}
    review: list[tuple[str, int, str]] = []
    week_ago = datetime.now(KST) - timedelta(days=7)
    for th in dc.forum_threads(ch["pass_forum"], active):
        if th.get("owner_id") != bot_id:
            continue
        msgs = dc.messages(th["id"])
        starter = next((m for m in msgs if m["id"] == th["id"]), None)
        if starter is None:
            try:
                starter = dc.get(f"/channels/{th['id']}/messages/{th['id']}")
            except RuntimeError:
                continue
        tag = USER_TAG.search(starter.get("content", ""))
        if not tag or tag.group(1) in posts:
            continue
        uid = tag.group(1)
        posts[uid] = (th, starter)
        if uid not in scores:
            continue
        for msg in msgs:
            num = MISSION_NO.match(msg.get("content", ""))
            mission = by_no.get(int(num.group(1))) if num else None
            if (msg["author"]["id"] != uid or not mission or not is_user_message(msg)
                    or not season.contains(msg["id"])
                    or not proof_ok(msg, mission, uid, admins)
                    or dc.reacted_by(th["id"], msg, REJECT, admins)):
                continue
            scores[uid].missions.add(mission["num"])
            if mission["proof"] == "image" and sent_at(msg["id"]) >= week_ago:
                review.append((uid, mission["num"], f"https://discord.com/channels/{gid}/{th['id']}/{msg['id']}"))

    # 블로그 후기: 운영진이 ✅를 단 글만
    for msg in dc.messages(ch["blog"]):
        uid = msg["author"]["id"]
        if (uid in scores and is_user_message(msg) and season.contains(msg["id"])
                and dc.reacted_by(ch["blog"], msg, APPROVE, admins)):
            scores[uid].blogs += 1

    # 합계와 순위 (동점이면 참여한 종목 수가 많은 사람이 앞)
    parts = {uid: sc.parts(rules) for uid, sc in scores.items()}
    total = {uid: sum(p) for uid, p in parts.items()}
    kinds = {uid: sum(v > 0 for v in p) for uid, p in parts.items()}
    ranking = sorted(scores, key=lambda u: (-total[u], -kinds[u], names[u]))

    # 1) 패스 글: 없는 디퍼는 새로 만들고, 있으면 보관 해제·제목·현황판·멤버 정리
    created = updated = cleaned = 0
    for uid in dippers:
        board = pass_board(uid, scores[uid], missions, rules)
        title = names[uid][:100]
        if uid not in posts:
            th = dc.write("POST", f"/channels/{ch['pass_forum']}/threads", {
                "name": title,
                "auto_archive_duration": 10080,
                "message": {"content": board, "allowed_mentions": {"users": [uid]}},
            })
            if th:
                dc.write("PUT", f"/channels/{th['id']}/thread-members/{uid}")
            created += 1
            continue
        th, starter = posts[uid]
        changes = {}
        if th.get("thread_metadata", {}).get("archived"):
            changes["archived"] = False
        if th["name"] != title:
            changes["name"] = title
        if changes:
            dc.write("PATCH", f"/channels/{th['id']}", changes)
        if starter["content"] != board:
            dc.write("PATCH", f"/channels/{th['id']}/messages/{starter['id']}",
                     {"content": board, "allowed_mentions": NO_PINGS})
            updated += 1
        # 멘션받은 사람은 디스코드가 글 멤버로 넣어서 사이드바에 남의 패스 글이 쌓인다.
        # 주인만 남기고 빼서, 사이드바엔 각자 자기 글만 보이게 한다.
        joined = {tm["user_id"] for tm in dc.get(f"/channels/{th['id']}/thread-members")}
        for other in joined - {uid, bot_id}:
            dc.write("DELETE", f"/channels/{th['id']}/thread-members/{other}")
            cleaned += 1
        if uid not in joined:
            dc.write("PUT", f"/channels/{th['id']}/thread-members/{uid}")

    # 2) 패스 뱃지 역할: 디퍼는 현재 단계 역할 하나만, 디퍼가 아닌 사람(운영진 등)은 전부 뗀다
    stage_roles = roles["pass_stages"]
    role_changes = 0
    for m in members:
        uid = m["user"]["id"]
        stage = scores[uid].stage(rules) if uid in scores else 0
        want = stage_roles[stage - 1] if stage else None
        have = set(m["roles"]) & set(stage_roles)
        for role in have - {want}:
            dc.write("DELETE", f"/guilds/{gid}/members/{uid}/roles/{role}")
            role_changes += 1
        if want and want not in have:
            dc.write("PUT", f"/guilds/{gid}/members/{uid}/roles/{want}")
            role_changes += 1

    # 3) 리더보드: 상위 N명 이름만
    size = cfg.get("leaderboard_size", 5)
    top = [u for u in ranking if total[u] > 0][:size]
    leaderboard = [f"🏆 **활동 포인트 TOP {size}**", ""]
    leaderboard += [f"{i}. {escape(names[u])}" for i, u in enumerate(top, 1)] or ["아직 집계된 점수가 없어요!"]
    dc.sync_messages(ch["leaderboard"], bot_id, ["\n".join(leaderboard)])

    # 4) 점수-집계 (운영진 전용): 전체 점수표 + 3단계 달성자 + 최근 7일 스샷 인증
    rows = [
        " · ".join([f"{i}. {escape(names[u])} **{total[u]}점**"]
                   + [f"{label} {v}" for label, v in zip(SCORE_LABELS, parts[u]) if v])
        for i, u in enumerate(ranking, 1)
    ]
    finishers = [escape(names[u]) for u in ranking if scores[u].stage(rules) == len(rules["pass_stages"])]
    report = [f"📋 **활동 포인트 집계** · {datetime.now(KST):%m/%d %H:%M} 기준 · 디퍼 {len(dippers)}명"]
    report += chunk(rows)
    report.append("🏅 **패스 3단계 달성자** · " + (", ".join(finishers) or "아직 없어요"))
    review_lines = [f"📷 **최근 7일 스샷·사진 인증** · 이상하면 해당 메시지에 {REJECT}"]
    review_lines += [f"- {escape(names[u])} · {no}번 → {link}" for u, no, link in review] or ["없어요"]
    report += chunk(review_lines)
    dc.sync_messages(ch["admin_report"], bot_id, report)

    print(f"디퍼 {len(dippers)}명 · 패스 글 생성 {created} · 현황판 갱신 {updated} · "
          f"글 멤버 정리 {cleaned} · 뱃지 변경 {role_changes} · 쓰기 요청 {dc.writes}")
    if dc.dry_run:  # 로컬 확인용. GitHub 로그에는 이름을 남기지 않는다
        print("\n[리더보드]\n" + "\n".join(leaderboard))
        print("\n[점수표]\n" + "\n".join(rows))
        if dippers:
            first = ranking[0]
            print(f"\n[{names[first]} 현황판 미리보기]\n" + pass_board(first, scores[first], missions, rules))


# ── 설정 ─────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [f"channels.{k}" for k, v in cfg["channels"].items() if not v]
    if not cfg.get("guild_id"):
        missing.insert(0, "guild_id")
    if not cfg["roles"].get("admins") or not all(cfg["roles"]["admins"]):
        missing.append("roles.admins")
    stage_roles = cfg["roles"].get("pass_stages") or []
    if len(stage_roles) != len(cfg["points"]["pass_stages"]) or not all(stage_roles):
        missing.append("roles.pass_stages")
    if missing:
        sys.exit("config.yaml에 비어 있는 값이 있어요: " + ", ".join(missing))

    # 디스코드 ID는 문자열로 통일 (따옴표 없이 적어도 동작하게)
    cfg["guild_id"] = str(cfg["guild_id"])
    cfg["channels"] = {k: str(v) for k, v in cfg["channels"].items()}
    cfg["roles"] = {k: [str(r) for r in v] for k, v in cfg["roles"].items()}
    for key in ("start", "end"):
        value = cfg["season"][key]
        cfg["season"][key] = value if isinstance(value, date) else date.fromisoformat(str(value))
    return cfg


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="디프만 활동 포인트 집계")
    parser.add_argument("--dry-run", action="store_true", help="디스코드에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    load_dotenv()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN이 없어요. 로컬은 .env 파일, GitHub는 Secrets를 확인해주세요.")
        return 1

    dc = Discord(token, args.dry_run)
    run(load_config(Path(__file__).with_name("config.yaml")), dc)
    if dc.errors:
        print(f"⚠️ 실패한 쓰기 요청 {len(dc.errors)}건")
        for e in dc.errors[:10]:
            print(" -", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
