# Codex 협업 방식 — BMO 로봇과 NEXUS를 분리한다

## 역할
- **BMO 담당 Codex:** BMO 로봇 원본 및 통합 기능. 얼굴 R10, 음성, RP2040, 카메라 등 기존 기능을 이어서 개발.
- **NEXUS 담당 Codex:** `GUNOBAE/BGH_BMO_NEXUS`에 한정. 점검 스크립트, 서버 배포/운영 코드, 모드 제어기와 단위 테스트를 개발.
- **이 채팅(설계/리뷰):** 사용자 요구 확정, 구현 순서, 공개 보안 경계, 실제 Pi 출력 검토, PR 리뷰, 초보자를 위한 명령어 설명.
- **사용자:** 실제 Pi 명령 실행과 결과 검수, 운영에 영향을 주는 변경 승인.

GitHub 이슈는 **작업 요청서**다. 이슈를 작성하는 것만으로 Codex가 자동 실행되지는 않는다. Codex를 NEXUS 저장소에서 시작하고 해당 이슈를 지시해야 한다.

## 작업 방식
1. 이 저장소의 `AGENTS.md`, 실측 자료 `docs/09_PI_BASELINE_KR.md`, 해당 GitHub 이슈를 읽는다.
2. NEXUS 저장소만 Worktree/독립 브랜치에서 수정하고, BMO 로봇 저장소는 수정하지 않는다.
3. **진단 → 문서/코드 작성 → 오프라인 테스트 → PR**. 현장 실행 및 호스트 설정 변경은 사용자가 별도로 승인한 뒤에 진행한다.
4. 변경된 기능은 PR 설명에 안전성·복구·테스트 증빙을 포함한다.
5. 머지 후 Pi의 NEXUS 폴더에서 `git pull --ff-only`. 갱신 전에 `git status`로 로컬 변경 여부 확인.

## 첫 3개 작업 단위
- **Task 01: Read-only inventory follow-up** — NVMe 0B 판정 보조, BMO 서비스/GUI 진입점 조사 가이드, 위험한 로그 비노출, `bash -n` 등 정적/모의 테스트. 현장 변화 0.
- **Task 02: BMO lifecycle contract** — BMO 담당 Codex로부터 *실제* 실행/정지 방법, 자식 프로세스 정리, 실패 시 처리 방법을 문서 형태로 받아 정리. 서버 코드에서 임의 `killall` 등을 사용하지 않는다.
- **Task 03: Mode controller mock** — 로봇/서버/정비 상태기계, lock, 전환 실패 시 fail-closed, SSD 필수 마운트 검사와 의사 서비스를 활용한 테스트. 현장 서비스 제어 미실행.

그다음에 저장장치 안정성 확인, BMO 백업, Docker localhost 시험, 사설 파일 클라우드, 공개 정적 사이트 순서로 진행한다.

## 단어 설명
- **Repository**: 코드와 변경 이력을 보관하는 프로젝트 저장소.
- **Branch**: 실험/작업을 기존 `main`에서 분리하는 줄기.
- **Worktree**: 같은 Git 저장소의 다른 작업 복사본. 서로 다른 Codex 세션이 동시에 파일을 수정할 때 유용.
- **Commit**: 코드의 특정 변경을 기록한 시점.
- **Pull Request(PR)**: 변경 내용을 `main`에 합칠지 검토하는 요청.
- **Merge**: 검수된 작업을 기준 브랜치에 합치는 것.

## 첫 번째 NEXUS Codex 입력 예시

> `GUNOBAE/BGH_BMO_NEXUS`를 대상으로 작업한다. 루트 `AGENTS.md`, `docs/09_PI_BASELINE_KR.md` 및 Task 01 GitHub 이슈를 먼저 읽어라. 기존 BMO 로봇 레포나 Pi의 설정/서비스는 절대 수정하지 말고, 읽기 전용 후속 점검 스크립트/설명과 모의 테스트를 작성하라. 단위 작업 브랜치를 만들고 테스트 결과와 위험성을 명시한 PR을 제안해라. 실제 Pi에서 실행하지 않은 검사는 통과했다고 주장하지 마라.
