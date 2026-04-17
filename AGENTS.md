# tabris

## 1. Project Overview

이 프로젝트는 **Slack Bolt (Socket Mode) 기반 봇**이다. 사용자 멘션 또는 DM에 응답해 Docker 컨테이너에서 Claude Code를 실행하고, 결과를 Slack 스레드로 돌려준다. Claude Code의 마크다운 출력은 `slack-markdown-parser`로 Block Kit(`markdown` / `table` 블록)에 맞게 변환해 게시한다.

구성 요소는 대략 다음과 같다.

* **`run_server.py`**: Socket Mode 연결, `app_mention`·DM `message` 처리, `docker run`으로 sandbox에서 Claude 실행
* **`Dockerfile` / `DOCKER_IMAGE`**: `hbsmith-claude-sandbox` 등 — `node:20-slim` 기반에 `@anthropic-ai/claude-code`를 올린 실행 환경
* **`_provisioning/configuration/docker/workspace`**: 스킬·`CLAUDE.md` 등을 스레드별 `/tmp/claude-sandbox/{thread_ts}`에 복사한 뒤 컨테이너 `/workspace`에 read-only 마운트
* **`_provisioning/`**: Vagrant(Parallels)로 VM 프로비저닝, systemd로 봇 상시 실행

Slack 앱 생성·스코프·이벤트·토큰·`BOT_USER_ID`·채널 초대, 로컬/Vagrant 실행, 환경 변수 표는 **README.md**를 따른다.

### Tech Stack
- **Python**: 3.12+ 권장(GitHub Actions 린트 기준), `slack-bolt`, `slack-markdown-parser`
- **Sandbox**: Docker(호스트에서 `docker` CLI로 컨테이너 실행)
- **Provisioning**: macOS + Parallels Desktop, Vagrant + `vagrant-parallels`, Amazon Linux 2023 계열 box
- **Code Quality**: ruff (linting & formatting)

### Python Runtime environment
* **로컬 개발**: 저장소 루트에서 `python3 -m venv venv` 후 `pip install -r _provisioning/requirements.txt` (README 「2. 로컬 Mac 개발」 참고)
* **프로비저닝 VM**: `/opt/tabris/venv` — `tabris.service`가 `ExecStart=/opt/tabris/venv/bin/python -u run_server.py`, `WorkingDirectory=/opt/tabris`, 설정 파일은 `TABRIS_SETTINGS=/etc/tabris/settings_local.py`

### Main Scripts

#### 봇 서버
* Run `python run_server.py` (로컬, venv 활성화 후) for Socket Mode로 Slack에 붙어 이벤트 처리
* VM에서는 `sudo systemctl {start|status|restart} tabris`, 로그는 `/var/log/tabris/run_server.log` (README 「3. Vagrant VM 실행」)

#### Claude sandbox 이미지
* Run `docker build -t hbsmith-claude-sandbox .` for README에 적힌 이미지명·`DOCKER_IMAGE`와 동일한 태그로 빌드

#### VM 코드 반영 / 이미지 재빌드
* Run `vagrant ssh -c 'cd /opt/tabris && sudo git pull && sudo systemctl restart tabris'` for 원격 브랜치 반영 후 봇 재시작
* Run `vagrant provision` for 프로비저닝 스크립트 자체 변경 시
* Run `vagrant ssh -c 'sudo docker build -t hbsmith-claude-sandbox /opt/tabris'` for Dockerfile·sandbox 이미지 갱신

### Lint and Reformat (Python)
코드 품질 관리를 위해 **ruff**를 사용하며, 설정은 `ruff.toml`을 따른다.

* **검사**: `ruff check .`
* **자동 수정**: `ruff check --fix .`
* **포맷팅**: `ruff format .`
* **개별 파일**: `ruff check <path>` / `ruff format <path>`

### Testing
REST API 및 WebSocket RPC에 대한 유닛테스트를 pytest로 실행할 수 있다.

#### 테스트 실행
```bash
# 전체 테스트 실행
pytest tests/ -v

# 특정 테스트 파일 실행
pytest tests/test_api_echo.py -v

# 병렬 테스트 실행 (4개 워커)
pytest tests/ -n 4 -v

# CPU 코어 수만큼 워커로 병렬 실행
pytest tests/ -n auto -v

# 커버리지 포함 실행
pytest tests/ --cov=run_web_server --cov-report=html
```

#### 테스트 구조
| 파일 | 설명 |
|------|------|
| `tests/conftest.py` | 공통 fixtures (DB, Mock 설정) |
| `tests/helpers.py` | WebSocket 테스트 헬퍼 함수 |
| `tests/test_api_*.py` | REST API 엔드포인트 테스트 |
| `tests/test_ws_*.py` | WebSocket RPC 테스트 |

#### 테스트 환경
* PostgreSQL 테스트 스키마 `hbsmith_test_*` 사용 (운영 `hbsmith`와 분리)
* 병렬 테스트 시 워커별 독립 스키마 생성/삭제
* 외부 의존성(OAuth, OpenAI 등)은 Mock으로 대체

---

## 2. Company R&D Principles

설계 및 구현 시 우선순위가 충돌하거나 판단이 필요할 때, 아래 원칙을 최우선으로 준수한다.

### Development Priorities & Problem Solving
* **Speed**: 빠른 개발 속도를 최우선으로 한다.
* **Simplicity (Occam's Razor)**: 간단하고 명료하게 설명 가능한 해결 방법을 선택한다.
* **Managed Service**: 동일 목적의 기능을 직접 구현하기보다, 가능한 한 Managed Service를 활용한다.
* **No-Code First**: 코드를 작성하지 않고 해결할 수 있는 방법이 있다면 그 방법을 우선한다.

### Technical Debt & Quality
* **Bug Fix Policy**: 실행되지 않은(발현되지 않은) 잠재적 버그는 현재 수정 대상이 아니다.
* **Code Duplication**: 낮은 의존성과 높은 응집성을 유지할 수 있다면 코드 중복을 허용한다.
* **Debt Management**: 기술 부채 처리는 가능한 한 뒤로 미룬다.
* **System Quality (3R)**: 모든 시스템은 다음 3가지 기준을 만족해야 한다.
  1. **Repeatability**: 동일 환경에서 반복 가능해야 한다.
  2. **Reproducibility**: 다른 환경에서도 동일하게 재현 가능해야 한다.
  3. **Reliability**: 과거, 현재, 미래의 결과가 일관되어야 한다.

### Planning & Communication
* **Scope**: 작업 범위는 Sprint 일정에 맞춰 조정한다.
* **Documentation**: 구두 전달보다는 기록(문서화)을 남긴다.
* **Tech Adoption**: 신기술 도입은 팀원 동의를 얻어야 하며, 기존 구현 옵션을 충분히 검토한 후에 진행한다. (도입 시 중복 로직 제거 필수)

### Testing
* 구현과 동시에 테스트 코드를 작성한다.
* 가능하면 제품을 직접 사용하여 검증(Dogfooding)한다.

---

## 3. Coding Convention

개발팀의 표준 코딩 컨벤션으로, 코드 작성 및 리팩토링 시 반드시 준수해야 한다.

### General Guidelines
* 코드베이스는 항상 깨끗하고 체계적으로 유지한다.
* **스크립트 지양**: 모듈 파일 내에 실행 가능한 스크립트 코드(top-level execution) 작성을 피한다. 특히 일회성 실행 코드는 주의한다.
* **Context Focus**: 작업과 관련된 코드 영역에만 집중하며, 관련 없는 코드는 수정하지 않는다.

### Naming Convention
* **No Shadowing**: Shadow naming 경고 발생 시 `_` 추가 또는 변수명 변경으로 해결한다.
* **No Reuse**: 삭제된 클래스/테이블/메서드 이름은 재사용하지 않고, 항상 새로운 이름을 부여한다.

### Resource Model Principle (API & DB)
* **1:1 Mapping**: API 리소스는 DB 테이블과 1:1로 대응한다.
* **Path Structure**: 리소스 경로는 DB 테이블 간의 FK(Foreign Key) 관계를 그대로 반영한다.
* **Pluralization**: 리소스 이름은 항상 **복수형(Plural)**을 사용한다.
```text
  # 예시
  /users/<user_id>/jobs/<job_id>/scripts/<script_id>
  /tables/<tableA_id>/columns/<column_id>
```

### API Path Convention

* **Word Class**: 경로는 **명사와 형용사**만 사용하며, 동사(Verb)는 포함하지 않는다. 행위는 HTTP Method로 표현한다.
* **Method Strategy**:
  * 가능한 경우 `POST` 대신 **`GET` 경로 확장 방식**을 우선 검토한다.
  * `POST`는 "기존 리소스에서 파생된 새로운 리소스"를 생성할 때만 사용한다.
  * 신규 생성 경로는 `/new/` 접미사를 사용한다.
  * 전체 조회 경로는 `/all/` 접미사를 사용한다.

```text
# 예시
(O) GET /visual-tests/<hash_key>/script_path/new/   (신규 생성 화면/폼 등)
(O) DEL /visual-tests/all/results/old/              (과거 테스트 일괄 삭제)
(O) GET /visual-tests/<hash_key>/script_path/all/   (전체 조회)

```

### Error & Exception Handling

* Try/Catch 블록은 에러 발생 예상 지점 기준으로 라인 단위로 분리한다.
* **All or Nothing**: 일부 성공/일부 실패는 허용하지 않는다. 모든 작업은 **모두 성공하거나 모두 실패**해야 한다.

### Reproducibility & Consistency

* 동일 기능은 동일한 API 의미 체계를 유지한다.
* 기능이 유사하다면 경로 구조와 리소스 모델의 일관성을 유지하며, 중복이 발생하더라도 의미적 구조를 임의로 변경하지 않는다.

### Refactoring & Testing Constraints

* **Function Granularity**: 2곳 이상에서 사용되는 경우가 아니라면 불필요한 함수 분리를 지양한다. 20줄 미만의 코드는 중복되더라도 함수로 분리하지 않는 것을 권장한다.
* **Mocking Policy**:
  * Mock Data는 개발(DV) 및 테스트 환경에서만 사용한다.
  * **검증(QA) 및 운영(OP) 환경에서는 절대 Mock/Stub 데이터를 사용하거나 코드에 포함해서는 안 된다.**
