# 저장장치와 백업 전략

## 현재 알 수 있는 것 / 없는 것

확정: Raspberry Pi 5 8 GB, microSD 512 GB, 장착 SSD 1 TB. **미확정:** 어느 디스크로 부팅하는지, SSD 파티션/파일시스템/마운트 지점, 현재 데이터 사용량. 그러므로 포맷/OS 이전/마운트 파일 자동 수정은 금지한다.

```text
Pi (example only; actual paths must be discovered)
  OS & BMO files: current root volume (unknown SD or SSD)
  /srv/bmo-nexus or another VERIFIED SSD mount
     vault/       private files
     mirrors/     GitHub bare mirrored repositories
     appdata/     service databases and configs
     sites/       approved public-only static site data
     archives/    local snapshot staging, NOT sole backup
```

- 경로는 **예시**이며 실제 `lsblk`, `findmnt`, `df -hT` 확인 후 확정한다.
- `vault`와 `sites`는 같은 디스크더라도 권한과 컨테이너 마운트를 따로 둔다.
- 저장공간 전체를 미리 파티션 5개로 쪼갤 필요는 없다. 폴더와 할당량으로 우선 운영한다.
- 웹 DB/업로드 같은 잦은 쓰기는 확인된 SSD에 배치하고, SD가 부팅 장치라면 사용 목적에 맞춰 쓰기량을 관리한다.
- 같은 Pi 안의 SSD ↔ SD 복사만으로는 도난/전원/침수 등에 대응한 완전한 백업이 아니다.

## GitHub 백업의 3단계

**A. Git mirror:** Git 커밋, 브랜치, 태그 등 Git 객체를 복사한다.

```bash
# 사용 방법 참고 예시. 실제 대상 경로/권한 확인 후 실행.
git clone --mirror https://github.com/OWNER/REPOSITORY.git REPOSITORY.git
# 이후 변경된 원격 참조를 확인해 mirror 업데이트 (삭제 반영 주의)
git --git-dir=REPOSITORY.git remote update --prune
```

`--prune`은 삭제된 원격 브랜치를 미러에서도 지울 수 있다. 따라서 **버전 보존이 필요하면 미러 업데이트 전에/후에 시점별 스냅샷**을 두어야 한다.

**B. 암호화 스냅샷:** restic으로 파일 디렉터리를 암호화·중복 제거된 스냅샷에 보관한다. 비밀번호를 GitHub에 업로드하지 않는다.

**C. 외부 사본 및 복원:** 다른 컴퓨터/외장 디스크/원격 저장소에 사본을 보관. `restic snapshots`, `restic check`, 복원 테스트를 실시한다.

### Git mirror에 포함되지 않는 것

GitHub Issues/PR 본문·댓글, Actions 실행 결과 및 Secret, Release 업로드 바이너리, GitHub 프로젝트 보드, 일부 Wiki는 별도 API/Export/백업 방법이 필요하다. GitHub의 모든 것을 `git clone --mirror`로 보존했다고 주장하지 않는다.

## 개인 파일 백업

- `vault/`는 매일/서버 모드 진입 시 증분 스냅샷 대상.
- 휴대폰에서 삭제한 파일을 바로 동기화 삭제하지 않도록 삭제 보존 정책을 고려한다.
- 복원 목표(RPO/RTO)는 개인 필요에 맞춰 설정하고, 최소 한 번 다른 디렉터리에 복구 시험.
- 백업 비밀번호 분실 시 복원 불가. 관리 암호·복구 키는 별도 안전한 장소에 보관.

## 무엇부터 검증하나?

1. SSD/SD 어디가 `/`인지 확인.
2. SSD 실제 데이터·공간과 mountpoint 확인.
3. 서버 폴더 구조 설계와 권한 분리.
4. 샘플 파일→백업→복원.
5. BMO 원본 아카이브와 개인 파일의 외부 사본.
