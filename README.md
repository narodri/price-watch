# price-watch — Ulike AirPro S 가격 감시

楽天市場 / Yahoo!ショッピング에서 **Ulike AirPro S**(UI04S, 정가 49,800엔)의 실질가를
하루 3회(JST 09:00 / 13:00 / 21:00) 수집하고, **역대 최저가(ATL)를 갱신하면** Gmail로 알린다.
이력은 `docs/history.jsonl`에 누적되고 GitHub Pages 대시보드에서 그래프로 볼 수 있다.

사양은 [SPEC.md](SPEC.md) 참조.

## 구성

```
price-watch/
├── watch.py                        # 본체 (표준 라이브러리만 사용, 의존성 없음)
├── test_e2e.py                     # API 응답을 스텁해 실행 흐름 전체를 도는 테스트
├── SPEC.md                         # 사양서
├── docs/
│   ├── index.html                  # 대시보드 (GitHub Pages)
│   └── history.jsonl               # 가격 이력 (자동 생성/커밋)
├── state.json                      # ATL, 헬스 카운터 (자동 생성/커밋)
└── .github/workflows/price-watch.yml
```

## 셋업

### 1. 이 디렉터리를 리포지토리 루트로 만든다

워크플로(`.github/workflows/`)와 Pages 소스(`/docs`)가 **리포지토리 루트 기준**이므로,
`price-watch/`를 그대로 새 리포지토리의 루트로 올려야 한다.

```bash
cd price-watch
git init -b main
git add .
git commit -m "초기 커밋: price-watch"
gh repo create price-watch --private --source=. --push
```

### 2. API 키 발급

| 키 | 발급처 | 비고 |
|---|---|---|
| `RAKUTEN_APP_ID` | https://webservice.rakuten.co.jp/ | 앱 등록 후 applicationId |
| `YAHOO_CLIENT_ID` | https://e.developer.yahoo.co.jp/register | Client ID (앱ID) |

둘 중 하나만 있어도 동작한다(있는 쪽만 조회).

### 3. Gmail 앱 패스워드

Google 계정 → 보안 → 2단계 인증 활성화 → 앱 비밀번호에서 16자리 발급.
일반 계정 비밀번호로는 SMTP 로그인이 안 된다.

### 4. GitHub Secrets 등록

Settings → Secrets and variables → Actions → New repository secret:

| 이름 | 필수 | 설명 |
|---|---|---|
| `RAKUTEN_APP_ID` | △ | 楽天 applicationId |
| `YAHOO_CLIENT_ID` | △ | Yahoo Client ID |
| `GMAIL_USER` | O | 발신 주소 |
| `GMAIL_APP_PASSWORD` | O | 앱 패스워드 16자리 |
| `MAIL_TO` | - | 수신 주소 (미지정 시 `GMAIL_USER`) |

△: 둘 중 최소 하나.

### 5. GitHub Pages

Settings → Pages → Source를 `main` 브랜치의 `/docs`로 지정.
**공개 페이지가 되므로 `docs/` 아래에는 비밀 정보를 절대 넣지 말 것.**
(대시보드는 `history.jsonl`만 읽는다. 여기엔 가격·상품명·URL만 들어간다.)

### 6. 첫 실행

Actions 탭 → price-watch → Run workflow.
첫 실행은 baseline만 기록하고 "baseline 설정 완료" 메일 1통을 보낸다(가격 알림 아님).
이 메일이 오면 API 키와 Gmail 설정이 모두 정상이라는 뜻이다.

## 로컬 실행

```bash
python watch.py --selftest    # 파싱/필터/헬스 로직 (네트워크 불필요)
python test_e2e.py            # ATL 갱신·콜드스타트·API 장애·dry-run 분기 전체
```

둘 다 워크플로에서도 매 실행 전에 돌린다.

```bash
RAKUTEN_APP_ID=xxx python watch.py --dry-run
```

`--dry-run`은 메일도 안 보내고 `state.json` / `history.jsonl`도 건드리지 않는다.

### ATL 수동 조작

오탐으로 ATL이 비정상적으로 낮게 박히면 이후 알림이 영영 안 온다.
알림 메일에 상품명 전문이 들어가므로, 본체가 아닌 게 잡혔다면 아래로 복구한다.

```bash
python watch.py --show-atl
python watch.py --reset-atl              # 삭제. 다음 실행에서 baseline 재설정
python watch.py --set-atl 29800          # 수동 지정
```

## 실행 로그 읽는 법

```
[2026-09-08 09:00 JST] Ulike AirPro S
  楽天  : 実質 31,860円 (定価 49,800円) / 候補 12件 / クーポン解析成功  9件
  Yahoo : 実質 32,800円 (定価 49,800円) / 候補  8件 / クーポン解析成功  6件
  最安  : 31,860円 (楽天) ULIKE CARE
  ATL   : 26,892円 (2026-07-05)
  -> 更新なし
```

- **候補 0件이 계속되면** 검색어나 필터가 죽은 것 → 3회 연속이면 경고 메일
- **クーポン解析成功 0件이 계속되면** 스토어 표기가 바뀐 것 → 7일 연속이면 경고 메일
  (세일이 없는 시기에도 0건이 될 수 있다. 메일이 오면 실제 상품명을 확인할 것)

## 알림 규칙

- ATL은 **사이트 통합**으로 하나만 관리한다. `history.jsonl`에는 사이트별로 기록한다.
- 현재 최저가 < ATL 이면 ATL을 갱신한다.
- 단 **갱신폭이 500엔 미만(`MIN_DROP_YEN`)이면 ATL만 갱신하고 메일은 보내지 않는다.**
- 첫 실행은 baseline만 기록하고 가격 알림을 보내지 않는다.

메일 트리거는 3종: ATL 갱신 / baseline 설정 / 헬스 경고.

## 실질가의 정의와 한계

API가 반환하는 `itemPrice`·`price`는 쿠폰 적용 전 가격인 경우가 대부분이다.
Ulike 공식점은 상품명에 쿠폰가를 써넣으므로 이를 파싱한다.

```
실질가 = min(API 가격, 상품명에서 파싱한 쿠폰가)
```

**검지할 수 없는 것**(사양상 인정):

- 상품명에 쿠폰가를 쓰지 않는 스토어 → 정가로만 인식한다. **놓칠 수 있다.**
- 타임세일 / 쇼핑몰 전체 쿠폰 → 미반영
- 포인트 환원(라쿠텐 SPU, PayPay) → 미반영. 계정마다 조건이 달라 일반화 불가
- Amazon 체크박스 쿠폰, メルカリ → 수집 자체를 하지 않는다(공식 API 없음)

**오탐 방어**(`watch.py` 상단 상수):

- 상품명에 `AirPro S` / `AirProS` / `Air Pro S` 중 하나가 있어야 함(전각·공백 정규화 후 비교)
- 카트리지·케이스·중고·병행수입 등이 들어가면 제외
- 실질가가 18,000엔(`SANE_MIN`) 미만 또는 80,000엔(`SANE_MAX`) 초과면 제외
- 재고 있는 상품만(楽天 `availability == 1`, Yahoo `inStock`)

## 운영상 알아둘 것

- **cron은 정시를 보장하지 않는다.** 수 분~수십 분 지연은 정상.
- 리포지토리에 60일간 활동이 없으면 스케줄 워크플로가 자동 비활성화되지만,
  매 실행마다 `history.jsonl`을 커밋하므로 문제없다.
- API 장애 시 한쪽이 죽어도 다른 쪽으로 진행한다. 양쪽 다 실패하면
  그 회차는 아무것도 기록하지 않고 종료한다(실패를 이력에 남기지 않는다).
- AirPro S가 구형이 되면 상시 저가가 되어 메일이 잦아질 수 있다.
  자동 대응은 하지 않으니, 잦아지면 감시를 종료하거나 대상을 바꿀 것.

## 아직 검증되지 않은 것 (첫 실행에서 확인할 것)

API 키가 없어 **실제 API 응답으로는 검증하지 못했다.** 파싱·필터·분기 로직은
`test_e2e.py`가 스텁 응답으로 전부 돌려 확인했지만, 아래 4가지는 첫 실행 로그를 봐야 안다.

1. **楽天 `itemName`에 쿠폰 표기가 실제로 들어오는지** — `クーポン解析成功` 건수가
   세일 기간에도 0이면 `COUPON_PATTERNS`를 실제 상품명에 맞게 고칠 것.
2. **`must_include` 필터가 과한지** — `候補 0件`이 계속되면 `AirPro S` 표기가
   예상과 다른 것이다(정규화로 전각·공백·붙여쓰기는 이미 흡수한다).
3. **재고 필드** — 楽天 `availability`(int 1), Yahoo `inStock`(bool)을 가정했다.
   키 이름이나 타입이 다르면 재고 없는 상품이 섞여 ATL을 오염시킬 수 있다.
4. **비세일 기간** — 쿠폰이 없으면 정가 49,800엔이 잡히는 게 정상이다.

`docs/history.jsonl`은 빈 파일로 커밋해 뒀다(첫 실행 전에도 대시보드가 에러 대신
"기록 없음"을 보여주게 하기 위함).

## 하지 않는 것

자동 구매·장바구니 담기 / Amazon·メルカリ 수집 / 포인트 환원 계산 / 여러 모델 비교
(`TARGETS`는 배열이라 나중에 늘릴 수는 있다).
