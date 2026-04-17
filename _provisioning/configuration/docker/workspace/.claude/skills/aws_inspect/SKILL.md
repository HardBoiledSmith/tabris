---
name: aws_inspect
description: HBsmith AWS 계정에 대해 올바른 role chain(SSO → OrchestratorRole → InspectReadRole)을 통해 read-only AWS CLI 작업을 수행하는 스킬. "aws inspect", "aws_inspect", "계정 조회" 요청 시 사용.
tools: Bash
---

# AWS Inspect

HBsmith AWS 계정에 대해 올바른 role chain을 통해 read-only AWS CLI 명령을 실행한다.

## 필수 Role Chain 구조

```
(hbsmith-aiagent SSO) → OrchestratorRole (591379657681) → InspectReadRole (<TARGET_ACCOUNT_ID>)
```

**절대 금지:**
- SSO 프로파일에서 InspectReadRole 직접 assume 금지
- InspectReadRole에서 다른 InspectReadRole assume 금지
- Step 1 없이 Step 2 진행 금지
- chain 완료 전 AWS 작업 실행 금지

## Step 1: 사전 조건 확인

```bash
# AWS CLI 설치 확인
if ! command -v aws &>/dev/null; then
  echo "❌ AWS CLI가 설치되어 있지 않습니다."
  exit 1
fi

# SSO 프로파일 확인
if ! aws sts get-caller-identity --profile hbsmith-aiagent &>/dev/null; then
  echo "❌ hbsmith-aiagent SSO 프로파일이 유효하지 않습니다."
  echo "해결 방법: aws sso login --profile hbsmith-aiagent"
  exit 1
fi

echo "✅ AWS CLI 및 SSO 프로파일 확인 완료"
```

## Step 2: 필수 정보 수집

작업 시작 전 사용자에게 다음 정보를 요청한다:

> AWS Inspect 작업을 위해 아래 정보가 필요합니다:
> 1. **대상 계정 ID** (TARGET_ACCOUNT_ID) — Organizations 목록을 먼저 조회해서 보여줄 수도 있음
> 2. **실행할 AWS CLI 명령** (read-only만 허용)

대상 계정을 모르는 경우, Step 3에서 OrchestratorRole을 assume한 뒤 Organizations 목록을 먼저 조회하여 사용자에게 선택하게 한다.

## Step 3: Chain Step 1 — SSO → OrchestratorRole

```bash
ORCH=$(aws sts assume-role \
  --profile hbsmith-aiagent \
  --role-arn arn:aws:iam::591379657681:role/ai-agent/HBsmithAIAgent-InspectOrchestratorRole \
  --role-session-name orchestrator-session \
  --output json)

if [ $? -ne 0 ]; then
  echo "❌ OrchestratorRole assume 실패"
  echo "$ORCH"
  exit 1
fi

ORCH_KEY=$(echo $ORCH | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['AccessKeyId'])")
ORCH_SECRET=$(echo $ORCH | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SecretAccessKey'])")
ORCH_TOKEN=$(echo $ORCH | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SessionToken'])")
```

**Identity 검증 (필수):**

```bash
IDENTITY=$(AWS_ACCESS_KEY_ID=$ORCH_KEY AWS_SECRET_ACCESS_KEY=$ORCH_SECRET AWS_SESSION_TOKEN=$ORCH_TOKEN \
  aws sts get-caller-identity --output json)

ACCOUNT=$(echo $IDENTITY | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
ROLE=$(echo $IDENTITY | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")

if [ "$ACCOUNT" != "591379657681" ]; then
  echo "❌ OrchestratorRole identity 검증 실패: Account=$ACCOUNT"
  exit 1
fi

echo "✅ Chain Step 1 완료: $ROLE"
```

## Step 4: (선택) Organizations 계정 목록 조회

대상 계정을 아직 모르는 경우 Organizations 목록을 조회한다.

**중요:** `organizations:ListAccounts` 권한은 **InspectReadRole (billing 591379657681) 에만** 있다.
- OrchestratorRole에서 직접 호출하면 AccessDenied 발생
- 반드시 Step 5에서 billing(591379657681) InspectReadRole을 assume한 뒤 호출할 것

```bash
# TARGET_ACCOUNT_ID=591379657681 로 Step 5를 먼저 완료한 뒤:
AWS_ACCESS_KEY_ID=$TARGET_KEY AWS_SECRET_ACCESS_KEY=$TARGET_SECRET AWS_SESSION_TOKEN=$TARGET_TOKEN \
  aws organizations list-accounts \
  --query 'Accounts[?Status==`ACTIVE`].[Id,Name,Email]' \
  --output table
```

목록을 보여주고 사용자에게 TARGET_ACCOUNT_ID를 선택하게 한다.

## Step 5: Chain Step 2 — OrchestratorRole → InspectReadRole

```bash
TARGET=$(AWS_ACCESS_KEY_ID=$ORCH_KEY AWS_SECRET_ACCESS_KEY=$ORCH_SECRET AWS_SESSION_TOKEN=$ORCH_TOKEN \
  aws sts assume-role \
  --role-arn arn:aws:iam::<TARGET_ACCOUNT_ID>:role/ai-agent/HBsmithAIAgent-InspectReadRole \
  --role-session-name inspect-read-session \
  --output json)

if [ $? -ne 0 ]; then
  echo "❌ InspectReadRole assume 실패 (계정: <TARGET_ACCOUNT_ID>)"
  echo "$TARGET"
  exit 1
fi

TARGET_KEY=$(echo $TARGET | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['AccessKeyId'])")
TARGET_SECRET=$(echo $TARGET | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SecretAccessKey'])")
TARGET_TOKEN=$(echo $TARGET | python3 -c "import sys,json; print(json.load(sys.stdin)['Credentials']['SessionToken'])")
```

**Identity 검증 (필수):**

```bash
IDENTITY2=$(AWS_ACCESS_KEY_ID=$TARGET_KEY AWS_SECRET_ACCESS_KEY=$TARGET_SECRET AWS_SESSION_TOKEN=$TARGET_TOKEN \
  aws sts get-caller-identity --output json)

ACCOUNT2=$(echo $IDENTITY2 | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
ROLE2=$(echo $IDENTITY2 | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")

if [ "$ACCOUNT2" != "<TARGET_ACCOUNT_ID>" ]; then
  echo "❌ InspectReadRole identity 검증 실패: Account=$ACCOUNT2"
  exit 1
fi

echo "✅ Chain Step 2 완료: $ROLE2"
```

## Step 6: AWS CLI 명령 실행

chain 완료 후 사용자가 요청한 명령을 실행한다. 모든 명령은 TARGET 자격증명을 사용한다.

**AWS CLI 호출 규칙 (필수):**
- 모든 호출에 `--output json` 추가 — 포맷 일관성 보장
- 모든 호출에 `--no-paginate` 추가 — nextToken + 옵션 충돌 방지
- python3으로 JSON 파싱 후 필요한 필드만 출력

```bash
AWS_ACCESS_KEY_ID=$TARGET_KEY \
AWS_SECRET_ACCESS_KEY=$TARGET_SECRET \
AWS_SESSION_TOKEN=$TARGET_TOKEN \
  <사용자가 요청한 AWS CLI 명령> --output json --no-paginate
```

**허용 명령 (read-only):**
- `aws cloudwatch list-metrics / get-metric-data / describe-alarms`
- `aws events list-rules / describe-rule`
- `aws logs describe-log-groups / describe-log-streams / filter-log-events`
- `aws ec2 describe-*`
- `aws s3 ls / s3api list-buckets / get-bucket-*`
- `aws iam get-* / list-*`
- `aws rds describe-*`
- `aws lambda list-*`
- `aws cloudtrail describe-trails`
- `aws organizations list-accounts`
- 기타 Describe* / List* / Get* 계열

**금지 명령 (write):**
- create / delete / update / put / attach / detach / modify / run / start / stop / terminate 계열
- 이 외 상태를 변경하는 모든 명령

write 명령 요청 시: "❌ 이 스킬은 read-only 명령만 허용합니다." 출력 후 중단.

## Step 7: 결과 보고

```
## AWS Inspect 결과

- 대상 계정: <TARGET_ACCOUNT_ID> (<계정명>)
- 실행 명령: <명령>
- Chain: hbsmith-aiagent → OrchestratorRole (591379657681) → InspectReadRole (<TARGET_ACCOUNT_ID>)
- 결과: 성공 / 실패

<명령 실행 결과>
```

## 오류 발생 시

어느 단계에서든 오류 발생 시 즉시 중단하고 아래를 출력한다:

```
❌ 오류 발생 (Step N)
실행 명령: <command>
오류 내용: <raw error>
```

재시도하거나 우회하지 않는다. 오류 원인을 분석하고 사용자에게 안내한다.
