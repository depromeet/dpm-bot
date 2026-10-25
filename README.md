# dpm-bot

디프만 활동 포인트 집계 봇. GitHub Actions에서 20분마다(매시 7·27·47분) 실행되어 디스코드 채널 기록을 처음부터 다시 읽고 점수를 계산한 뒤, 디프만 패스 현황판 · 리더보드 · 점수-집계 · 패스 뱃지를 갱신한다. 별도 DB 없이 디스코드가 곧 저장소다.

## 구성

| 파일 | 역할 |
|---|---|
| `collect.py` | 집계 스크립트 |
| `config.yaml` | 서버·채널·역할 ID, 시즌 기간, 배점, 미션 목록 |
| `.github/workflows/collect.yml` | 20분마다 자동 실행 + 수동 실행 버튼 |

배점이나 미션이 바뀌면 `config.yaml`만 고치면 된다.

## 로컬 실행

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # DISCORD_TOKEN= 뒤에 봇 토큰 입력
.venv\Scripts\python collect.py --dry-run   # 디스코드에 쓰지 않고 결과만 확인
.venv\Scripts\python collect.py             # 실제 반영
```

## 새 기수에 쓸 때

1. 디스코드 개발자 포털에서 봇을 새 서버에 초대 (`Message Content`, `Server Members` 인텐트 필요)
2. 봇 역할을 패스 뱃지 역할보다 위에 두기
3. 봇에게 예외 권한 주기: 점수-집계(보기·보내기·기록), 리더보드(보내기), 디프만-패스(포스트 만들기)
4. `config.yaml`의 ID와 시즌 기간 교체
5. 저장소 `Settings → Secrets and variables → Actions`에 `DISCORD_TOKEN` 등록
