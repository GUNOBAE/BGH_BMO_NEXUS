# START HERE — BMO NEXUS 처음 시작하기

## 이 ZIP 안에는 무엇이 들어 있나?

- GitHub 공개 README / 영어 README
- 구현 순서·학습 노트·모드 전환·보안·스토리지·BMO 통합 문서
- Raspberry Pi에서 현재 상태를 **읽기만 하는** `scripts/inspect_pi.sh`
- GitHub에 새 공개 저장소를 **한 번에 생성하고 업로드**하는 `scripts/publish_to_github.sh`

아직 BMO Pi를 변경하거나 서버를 설치하지 않는다.

## 선택 A: Pi(또는 GitHub CLI 로그인된 PC)에서 한 번에 새 저장소 만들기

1. 다운로드한 `BGH_BMO_NEXUS_READY.zip`을 Raspberry Pi의 다운로드 폴더로 옮긴다.
2. Pi 터미널에서 다음을 실행한다. ZIP 파일이 실제 `~/Downloads`에 있을 때의 예시다.

```bash
cd ~/Downloads
unzip BGH_BMO_NEXUS_READY.zip
cd BGH_BMO_NEXUS
gh auth status
bash scripts/publish_to_github.sh
```

- `gh auth status`가 로그인 오류라면 `gh auth login`부터 진행한다.
- `git config` 오류가 나면 본인이 설정할 이름/이메일을 사용해 다음처럼 설정한다(예시는 그대로 복사하지 말 것):

```bash
git config --global user.name "YOUR GITHUB NAME"
git config --global user.email "YOUR GITHUB EMAIL"
```

- 게시 확인 질문이 나오면 `CREATE` 입력. **GitHub에 공개 저장소** `GUNOBAE/BGH_BMO_NEXUS`가 만들어지고 초기 문서가 업로드된다.
- GitHub 계정 권한/네트워크 문제로 실패하면 출력 메시지를 확인하고, 임의의 `git push --force`나 저장소 삭제를 하지 않는다.

## 선택 B: 휴대폰 GitHub 웹에서 저장소부터 만들기

1. https://github.com/new 방문(계정 GUNOBAE로 로그인).
2. Repository name: `BGH_BMO_NEXUS` / Visibility: **Public**.
3. 초기 README, .gitignore, License 자동 생성은 하지 말고 **Create repository**를 누른다.
4. 생성된 저장소 주소를 대화에 알려 준다. 연결된 GitHub 도구에서 해당 새 저장소에 접근할 수 있는 경우, 준비된 문서를 이어서 등록할 수 있다. 해당 접근 권한이 없으면 위 CLI 방법을 사용한다.

**주의:** 이 문서만으로 실제 원격 GitHub 저장소가 이미 생성된 것은 아니다.

## GitHub 업로드를 마친 뒤 첫 BMO 작업

Pi에서 아래 명령으로 현재 상태만 진단한다.

```bash
cd ~/Downloads/BGH_BMO_NEXUS  # 실제 ZIP을 풀어놓은 위치에 맞춰 수정
bash scripts/inspect_pi.sh
```

출력을 공개 사이트에 붙여넣지 않는다. 내 주소/IP, 토큰, 경로, 개인 이름 등이 노출되지 않았는지 확인하고 필요 항목만 공유한다. 결과를 본 뒤 SSD/SD 마운트, BMO 앱 자동실행, 향후 서버 서비스 설치를 순서대로 진행한다.
