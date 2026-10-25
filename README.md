# dpm-bot

디프만 활동 포인트 집계 봇. GitHub Actions에서 20분마다(매시 7·27·47분) 실행되어 디스코드 채널 기록을 처음부터 다시 읽고 점수를 계산한 뒤, 디프만 패스 현황판 · 리더보드 · 점수-집계 · 패스 뱃지를 갱신한다. 별도 DB 없이 디스코드가 곧 저장소다.

## 구성

| 파일 | 역할 |
|---|---|
| `collect.py` | 집계 스크립트 |
| `setup_server.py` | 새 서버 세팅 (채널·역할·봇 권한·포럼 설정을 만들고 `config.yaml` ID를 채움) |
| `config.yaml` | 실제 서버의 ID, 시즌 기간, 배점, 미션 목록 (GitHub Actions가 사용) |
| `config.test.yaml` | 테스트 서버용 설정 (`--config config.test.yaml`) |
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

새 서버에 `🔒 운영` 카테고리, `운영진` 역할, 포럼 4개(오늘의-질문·번개·스터디·디프만-패스), `블로그-후기`·`리더보드` 채널을 먼저 만들어둔다.

1. 봇을 **관리자 권한 포함**으로 새 서버에 초대 (`Message Content`, `Server Members` 인텐트 필요)
2. `python setup_server.py --list` → 서버 ID 확인
3. `python setup_server.py --guild <서버ID>` → 바뀔 내용 미리보기
4. `python setup_server.py --guild <서버ID> --apply --season-start YYYY-MM-DD` → 적용 + `config.yaml` ID 자동 교체
5. 서버 설정에서 역할 순서를 `운영진 → 봇 → 패스 3단계 → 2단계 → 1단계`로 드래그 (봇은 자기 역할을 못 옮김)
6. 봇 역할의 **관리자 권한 끄기**
7. `config.yaml`의 시즌 종료일 확인 후 커밋·푸시
8. 저장소 `Settings → Secrets and variables → Actions`에 `DISCORD_TOKEN` 등록 (같은 봇이면 그대로)
