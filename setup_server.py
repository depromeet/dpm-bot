"""새 디스코드 서버에 활동 포인트용 설정을 넣고 config.yaml의 ID를 채운다.

이미 있는 것은 건드리지 않고 없는 것만 만든다. 가이드라인·채널 주제·숨김 기간만 덮어쓴다.
봇에 관리자 권한이 있어야 한다 (비공개 운영 카테고리에 채널을 만들어야 해서). 끝나면 관리자 권한은 회수.

    python setup_server.py --list                  봇이 들어가 있는 서버 목록
    python setup_server.py --guild <서버ID>         바꿀 내용 미리보기
    python setup_server.py --guild <서버ID> --apply 실제 적용 + config.yaml 갱신
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from collect import Discord

TEXT, CATEGORY, FORUM = 0, 4, 15
VIEW, SEND, HISTORY = 1 << 10, 1 << 11, 1 << 16
ONE_DAY, ONE_WEEK = 1440, 10080

# config 키 → (채널 이름에서 이모지를 뺀 이름, 종류)
CHANNELS = {
    "question_forum": ("오늘의-질문", FORUM),
    "bungae_forum": ("번개", FORUM),
    "study_forum": ("스터디", FORUM),
    "pass_forum": ("디프만-패스", FORUM),
    "blog": ("블로그-후기", TEXT),
    "leaderboard": ("리더보드", TEXT),
    "admin_report": ("점수-집계", TEXT),
    "question_queue": ("질문-예약", TEXT),
}
ADMIN_ROLE = "운영진"
DIPPER_ROLE = "19기 디퍼"  # 기본값. --dipper-role로 바꿀 수 있음. 색 없음: 색이 있으면 패스 뱃지 색을 가린다
OPS_CATEGORY = "운영"
# 없으면 운영 카테고리 안에 새로 만드는 운영진 전용 채널
NEW_CHANNELS = {"admin_report": "📋점수-집계", "question_queue": "📝질문-예약"}
PASS_ROLES = [("패스 1단계", 0xCD7F32), ("패스 2단계", 0xA8A9AD), ("패스 3단계", 0xD4AF37)]

# 봇이 디퍼에게 막힌 곳에서도 일할 수 있게 주는 예외 권한
BOT_ALLOW = {
    "admin_report": VIEW | SEND | HISTORY,
    "question_queue": VIEW | HISTORY,  # 예약된 질문 읽기
    "leaderboard": SEND,
    "pass_forum": SEND,
    "question_forum": SEND,  # 오늘의 질문 자동 게시
}

GUIDE_QUESTION = """🙋 평일마다 질문이 올라와요

질문 글을 열고 답을 남겨주세요.
예) 저는 보드게임 중에 스플렌더를 제일 좋아해요!

→ 이번 주에 한 번이라도 답하면 3점!"""

GUIDE_BUNGAE = """⚡ 번개 여는 법

1. 새 게시물로 모집 글을 올려요
   예) 제목: 11/8(토) 세션 끝나고 보드게임 🎲
       본문: 강남역 근처, 4명 모집! 오실 분 댓글 주세요

2. 번개가 끝나면 글을 연 사람이 이 글에 같이 간 사람을 멘션해주세요
   예) 오늘 함께한 분들 @김디퍼 @이디퍼 즐거웠어요!

→ 연 사람 20점, 멘션된 참여자 10점!
참여한 사람은 따로 할 거 없어요. 연 사람이 멘션해주면 점수가 들어가요.

📌 일주일 동안 글이 없으면 사이드바에서 숨겨지지만, 번개 채널에는 그대로 있어요."""

GUIDE_STUDY = """📚 스터디 여는 법

1. 새 게시물로 모집 글을 올려요
   예) 제목: [모집] 주 1회 CS 스터디 (4명)
       본문: 매주 수요일 저녁 온라인, 운영체제부터 시작해요!

2. 스터디가 시작되면 글을 연 사람이 이 글에 참여자를 멘션해주세요
   예) 첫 모임 완료! 함께하는 분들 @김디퍼 @이디퍼

→ 연 사람 30점, 멘션된 참여자 20점!
참여한 사람은 따로 할 거 없어요. 연 사람이 멘션해주면 점수가 들어가요.

📌 일주일 동안 글이 없으면 사이드바에서 숨겨져요. 진행 상황을 올리면 다시 떠요."""

TOPIC_BLOG = ("후기마다 메시지 하나씩, 주제 + 링크로 올려주세요. "
              "예) 1주차 세션 후기 https://velog.io/@... · 홍보부 확인 후 ✅가 달리면 점수에 반영돼요")
TOPIC_LEADERBOARD = "활동 포인트 상위 5명이 업데이트돼요"
TOPIC_QUEUE = ("질문만 쓰는 채널이에요. 한 줄에 'MM/DD 질문' (예: 11/2 가장 좋아하는 음식은?). "
               "같은 날짜를 다시 쓰면 나중에 쓴 질문이 올라가요. 대화는 다른 채널에서 해주세요!")

# config 키 → 바꿀 채널 설정 (topic은 포럼에선 게시물 가이드라인)
CHANNEL_SETTINGS = {
    "question_forum": {"default_auto_archive_duration": ONE_DAY, "topic": GUIDE_QUESTION},
    "bungae_forum": {"default_auto_archive_duration": ONE_WEEK, "topic": GUIDE_BUNGAE},
    "study_forum": {"default_auto_archive_duration": ONE_WEEK, "topic": GUIDE_STUDY},
    "pass_forum": {"default_auto_archive_duration": ONE_WEEK},
    "blog": {"topic": TOPIC_BLOG},
    "leaderboard": {"topic": TOPIC_LEADERBOARD},
    "question_queue": {"topic": TOPIC_QUEUE},
}


def bare(name: str) -> str:
    """'📌공지사항' → '공지사항'. 이모지·공백을 빼고 비교한다."""
    return re.sub(r"[^0-9A-Za-z가-힣-]", "", name)


def short(value) -> str:
    text = str(value if value is not None else "(없음)").replace("\n", " / ")
    return text if len(text) <= 60 else text[:60] + "…"


def plan_and_apply(dc: Discord, gid: str, apply: bool, dipper_role: str = DIPPER_ROLE) -> dict[str, str]:
    """바꿀 내용을 출력하고, apply면 적용한다. 찾거나 만든 ID를 돌려준다."""
    bot_id = dc.get("/users/@me")["id"]
    guild = dc.get(f"/guilds/{gid}")
    channels = dc.get(f"/guilds/{gid}/channels")
    roles = guild["roles"]
    print(f"서버: {guild['name']} ({gid})\n")

    def find(name: str, kind: int) -> dict | None:
        return next((c for c in channels if c["type"] == kind and bare(c["name"]) == name), None)

    ids: dict[str, str] = {"guild_id": gid}
    missing = []
    for key, (name, kind) in CHANNELS.items():
        found = find(name, kind)
        if found:
            ids[key] = found["id"]
        elif key not in NEW_CHANNELS:
            missing.append(f"#{name}")
    if missing:
        sys.exit("이 채널을 못 찾았어요: " + ", ".join(missing) + "\n채널 이름이 테스트 서버와 같은지 확인해주세요.")

    admin_role = next((r for r in roles if r["name"] == ADMIN_ROLE), None)
    if not admin_role:
        sys.exit(f"'{ADMIN_ROLE}' 역할을 못 찾았어요.")
    ids["admins"] = admin_role["id"]

    # 1) 운영진 전용 채널(점수-집계, 질문-예약): 운영 카테고리 권한을 그대로 따르게 만든다
    print("[채널]")
    for key, channel_name in NEW_CHANNELS.items():
        if key in ids:
            print(f"  {channel_name}: 이미 있음")
            continue
        ops = find(OPS_CATEGORY, CATEGORY)
        if not ops:
            sys.exit(f"'{OPS_CATEGORY}' 카테고리를 못 찾았어요.")
        print(f"  {channel_name}: 새로 만듦 (운영 카테고리 안, 운영진만 보임)")
        if apply:
            made = dc._request("POST", f"/guilds/{gid}/channels", json={
                "name": channel_name, "type": TEXT, "parent_id": ops["id"],
                "permission_overwrites": ops.get("permission_overwrites", []),
            })
            ids[key] = made["id"]

    # 2) 디퍼 역할 · 패스 역할: 없으면 만든다 (새 역할은 맨 아래에 생긴다)
    print("\n[역할]")
    dipper = next((r for r in roles if r["name"] == dipper_role), None)
    if dipper:
        print(f"  {dipper_role}: 이미 있음")
        ids["dippers"] = dipper["id"]
    else:
        print(f"  {dipper_role}: 새로 만듦 (색 없음, 권한 없음, 따로 표시)")
        if apply:
            ids["dippers"] = dc._request("POST", f"/guilds/{gid}/roles", json={"name": dipper_role, "permissions": "0", "hoist": True})["id"]
    pass_ids = []
    for name, color in PASS_ROLES:
        role = next((r for r in roles if r["name"] == name), None)
        if role:
            print(f"  {name}: 이미 있음")
            pass_ids.append(role["id"])
            continue
        print(f"  {name}: 새로 만듦 (권한 없음, 따로 표시)")
        if apply:
            made = dc._request("POST", f"/guilds/{gid}/roles", json={"name": name, "color": color, "hoist": True, "permissions": "0"})
            pass_ids.append(made["id"])
    if apply:
        ids["pass_stages"] = pass_ids
        # 봇은 자기 역할보다 아래 있는 역할만 옮길 수 있어서, 순서는 사람이 맞추고 여기선 확인만 한다
        current = {r["id"]: r for r in dc.get(f"/guilds/{gid}/roles")}
        bot_top = max(r["position"] for r in current.values() if r.get("tags", {}).get("bot_id") == bot_id)
        positions = [current[ids["dippers"]]["position"]] + [current[rid]["position"] for rid in pass_ids]
        if not (positions == sorted(set(positions)) and positions[-1] < bot_top):
            print(f"  ⚠️ 역할 순서를 서버 설정에서 맞춰주세요: "
                  f"운영진 → 디프만 봇 → 패스 3단계 → 2단계 → 1단계 → {dipper_role}")

    # 3) 봇 예외 권한
    print("\n[봇 예외 권한]")
    labels = {VIEW: "채널 보기", SEND: "메시지 보내기/포스트 만들기", HISTORY: "메시지 기록 보기"}
    for key, allow in BOT_ALLOW.items():
        names = ", ".join(v for bit, v in labels.items() if allow & bit)
        print(f"  #{CHANNELS[key][0]}: 디프만 봇에 {names} 허용")
        if apply:
            cid = ids[key]
            current = next((o for o in dc.get(f"/channels/{cid}")["permission_overwrites"] if o["id"] == bot_id), None)
            merged = allow | int(current["allow"]) if current else allow
            deny = int(current["deny"]) & ~merged if current else 0
            dc._request("PUT", f"/channels/{cid}/permissions/{bot_id}",
                        json={"type": 1, "allow": str(merged), "deny": str(deny)})

    # 4) 숨김 기간 · 가이드라인 · 채널 주제 (덮어쓰기)
    print("\n[채널 설정] 지금 값 → 바꿀 값")
    for key, settings in CHANNEL_SETTINGS.items():
        channel = next((c for c in channels if c["id"] == ids.get(key)), None)
        if channel is None:  # 이번에 새로 만든 채널은 아래에서 따로 설정
            if apply and key in ids:
                dc._request("PATCH", f"/channels/{ids[key]}", json=settings)
            print(f"  #{CHANNELS[key][0]}: 새 채널에 설정")
            continue
        changes = {k: v for k, v in settings.items() if channel.get(k) != v}
        if not changes:
            print(f"  #{CHANNELS[key][0]}: 이미 같음")
            continue
        for k, v in changes.items():
            label = {"default_auto_archive_duration": "숨김 기간(분)", "topic": "가이드라인/주제"}[k]
            print(f"  #{CHANNELS[key][0]} {label}: {short(channel.get(k))} → {short(v)}")
        if apply:
            dc._request("PATCH", f"/channels/{ids[key]}", json=changes)

    return ids


def update_config(path: Path, ids: dict, season_start: str) -> None:
    """주석은 살리고 값만 바꾼다."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'^guild_id: .*$', f'guild_id: "{ids["guild_id"]}"', text, flags=re.M)
    text = re.sub(r'^(\s*start: )\S+.*$', rf'\g<1>{season_start}   # 이 날부터 (KST)', text, flags=re.M)
    for key in CHANNELS:
        text = re.sub(rf'^(\s*{key}: )"[^"]*"', rf'\g<1>"{ids[key]}"', text, flags=re.M)
    text = re.sub(r'(admins:[^\n]*\n\s*- )"[^"]*"', rf'\g<1>"{ids["admins"]}"', text)
    text = re.sub(r'(dippers:[^\n]*\n\s*- )"[^"]*"', rf'\g<1>"{ids["dippers"]}"', text)
    stages = ids["pass_stages"]
    text = re.sub(r'(pass_stages:[^\n]*\n\s*- )"[^"]*"(.*\n\s*- )"[^"]*"(.*\n\s*- )"[^"]*"',
                  rf'\g<1>"{stages[0]}"\g<2>"{stages[1]}"\g<3>"{stages[2]}"', text)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="새 서버에 활동 포인트 설정 넣기")
    parser.add_argument("--list", action="store_true", help="봇이 들어가 있는 서버 목록")
    parser.add_argument("--guild", help="설정할 서버 ID")
    parser.add_argument("--apply", action="store_true", help="실제로 적용 (없으면 미리보기)")
    parser.add_argument("--season-start", default="2026-10-31", help="config에 넣을 시즌 시작일")
    parser.add_argument("--config", default="config.yaml", help="갱신할 설정 파일")
    parser.add_argument("--dipper-role", default=DIPPER_ROLE, help="디퍼 역할 이름 (예: 20기 디퍼)")
    args = parser.parse_args()

    load_dotenv()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN이 없어요. .env 파일을 확인해주세요.")
        return 1
    dc = Discord(token, dry_run=not args.apply)
    if args.list or not args.guild:
        for g in dc.get("/users/@me/guilds"):
            print(g["id"], g["name"])
        return 0

    ids = plan_and_apply(dc, args.guild, args.apply, args.dipper_role)
    if not args.apply:
        print("\n미리보기예요. 괜찮으면 --apply를 붙여서 다시 실행하세요.")
        return 0
    update_config(Path(__file__).with_name(args.config), ids, args.season_start)
    print(f"\n적용 완료. {args.config}에 ID를 채웠어요. 봇 역할의 관리자 권한을 꺼주세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
