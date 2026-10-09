# MultiDot 간단 설정

사용자가 입력할 것은 dot마다 **이름, tunnel ID, runtime API key**입니다.
내부 producer/worker 토큰과 포트는 `setup`이 준비합니다.

A/B/C라는 이름이나 3개 구성은 예시일 뿐입니다. 한글 등 원하는 표시 이름을
쓰고, `dots` 목록에 필요한 수만큼 항목을 넣으세요. 고정된 3/4/16개 제한은
없지만 설정 파일 크기, 사용 가능한 포트와 메모리 등의 한계는 있습니다.

> 현재는 코드와 로컬 검증 단계입니다. 실제 키로 setup이나 tunnel 연결을
> 실행하지 않았습니다. 대상은 사용자의 Mac이 아니라 **dot의 클라우드**이며,
> 사용자가 그곳의 파일을 안전하게 직접 편집하는 경로는 아직 확인되지
> 않았습니다. 그 경로가 마련되기 전에는 실제 키를 입력하지 마세요.

## 1. 빈 설정 만들기

실행할 Linux 환경의 저장소에서 사용자가 직접 실행합니다.
Python 3.11+와 검증된 고정 버전 도구 설치가 먼저 필요합니다.

```sh
python3 scripts/multidot.py init
```

`config/dots.private.json`을 소유자만 읽고 쓸 수 있게 생성합니다.
이미 있는 파일은 덮어쓰지 않습니다.

## 2. 파일을 직접 편집하기

신뢰하는 개인용 편집기로 `config/dots.private.json`을 엽니다.
빈 문자열을 채우고, dot을 더 쓸 경우 같은 형식의 항목을 목록에 추가하세요.

```json
{
  "schema_version": 1,
  "dots": [
    {"name": "조사 담당", "tunnel_id": "", "runtime_api_key": ""}
  ]
}
```

- `name`: 원하는 표시 이름. 이름이 역할이나 권한을 정하지 않습니다.
- `tunnel_id`: [OpenAI Tunnels 설정](https://platform.openai.com/settings/organization/tunnels)에서 확인한 기존 ID. 항목마다 서로 달라야 합니다.
- `runtime_api_key`: [OpenAI API keys 설정](https://platform.openai.com/settings/organization/api-keys)에서 준비한, 해당 tunnel에 **Read + Use** 권한이 있는 키. `Bearer `는 붙이지 않습니다. admin key는 쓰지 마세요.

하나의 키를 여러 항목에 써도 되지만, **그 키와 principal이 각 tunnel 및
관련 workspace/org에 접근할 권한을 모두 가져야 합니다**. 같은 계정이라는
이유만으로 모든 tunnel에 사용할 수 있는 것은 아닙니다.
[공식 권한 안내](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/permissions.md)

역할을 생략하면 모두 `worker`입니다. 결과 종합용 dot이 필요한 고급 구성만
해당 항목에 `"role": "synthesis"`를 추가하세요. 종합용은 최대 하나이고,
일반 `worker`가 최소 하나 필요합니다.

키가 들어간 파일을 채팅, Git, 스크린샷이나 공유 문서에 올리지 마세요.
agent에게 파일을 열거나 내용을 검사하도록 요청하지 말고 다음 명령도
사용자가 직접 실행하세요. 파일은 암호화되지 않은 평문입니다.

## 3. 한 번 준비하기

로컬 내부 자격 증명 생성까지 승인한 뒤 직접 실행합니다.

```sh
python3 scripts/multidot.py setup
```

설치된 고정 버전 도구를 검증한 뒤 내부 큐, 토큰, 포트, 상태 파일을
준비합니다. 도구를 다운로드하거나 OpenAI에 접속하지 않고, 새 tunnel을
만들거나 서비스를 시작하지 않습니다. 성공 메시지도 실제 계정 연결이나
키의 원격 권한이 확인됐다는 뜻은 아닙니다.

`reviewed_runtime_installation_missing`이 나오면 검증된 도구 설치가 먼저
필요합니다. 키를 다시 입력할 문제가 아닙니다.
[설치 전제와 실행 환경](persistent-runtime.md#prerequisites-and-paths)을 확인하세요.

## 4. 실행하기

해당 tunnel 연결과 실행이 승인된 환경에서 직접 실행합니다.

```sh
python3 scripts/multidot.py run
```

전체 구성을 **포그라운드**로 실행합니다. 실행 세션을 열어 두고, 중지는
그 세션에서 Ctrl-C를 누릅니다. 자동 부팅 서비스는 설치하지 않습니다.
세션이나 클라우드 호스트가 종료되면 계속 실행된다는 보장은 없습니다.

## 나중에 바꿀 때

- 처음 준비할 때 이름·개수·역할을 정하세요. 표시 이름 변경과 목록 순서 변경은 런타임을 멈춘 뒤 파일을 편집하고 `setup`을 다시 실행하면 됩니다. 내부 UUID, 큐, tenant와 키는 유지됩니다.
- 기존 설치에서 dot 추가/삭제, tunnel 변경, 역할 변경, 키 교체는 현재 `setup`이 거부합니다. 별도 이전·교체 절차가 필요하며, 이를 우회하려고 기존 상태를 삭제하지 마세요.
- 내부 토큰 유효기간은 30일입니다. 자동 갱신은 없으므로 만료 후 계속 운영하려면 명시적인 갱신 절차가 필요합니다.
- 각 dot의 실제 작업 실행, Events 연결과 사용자에게 결과 전달은 별도 검증이 필요합니다.

자세한 보관·재설정 주의사항은 [키 설정 안내](private-key-config.md),
운영과 복구 한계는 [런타임 안내](persistent-runtime.md)에 있습니다.
