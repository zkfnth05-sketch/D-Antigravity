# -*- coding: utf-8 -*-
"""
[보정 패치] 신규 파일 최초 스냅샷 기록

문제:
  save_file_content 는 기존 파일일 때만 _snapshot_before_save() 를 호출했다.
  따라서 신규 파일 생성 시점에는 스냅샷이 하나도 남지 않아
  timeline.jsonl 에 최초 상태가 등록되지 않았다.

해결:
  - 신규 파일(기존 파일 부재) 경로에서 저장 완료 '직후'
    create_backup(target, note="initial creation") 을 호출하여
    최초 스냅샷을 원장에 등록한다.
  - 기존 파일 수정 시에는 종전대로 '수정 직전 원본' 스냅샷(pre-edit)을 유지한다.

주의:
  snapshot_engine.create_backup 시그니처는 (target_path, note, force) 이며
  content 인자를 받지 않는다. 따라서 파일이 디스크에 기록된 뒤 호출한다.
"""
import io
import os
import sys

TARGET = os.path.join("backend", "core", "project_manager.py")

PATCHES = []


def patch(name, search, replace):
    PATCHES.append((name, search, replace))


# ----------------------------------------------------------------------
# 1) save_file_content — 신규 파일 여부 판정 + 저장 후 최초 스냅샷
# ----------------------------------------------------------------------
patch(
    "save-file-content-new-file-snapshot",
    """        # (3) 쓰기 직전 원본 읽기 + 자동 백업
        original_content = ""
        backup_path = ""
        if os.path.isfile(target):
""",
    """        # (3) 쓰기 직전 원본 읽기 + 자동 백업
        original_content = ""
        backup_path = ""
        is_new_file = not os.path.isfile(target)
        if not is_new_file:
""",
)

patch(
    "save-file-content-after-create-hook",
    """            with open(target, "w", encoding="utf-8", newline="\\n") as f:
                f.write(new_content)
            logger.info("[ProjectManager] 저장 완료: %s", target)
        except Exception as e:
""",
    """            with open(target, "w", encoding="utf-8", newline="\\n") as f:
                f.write(new_content)
            logger.info("[ProjectManager] 저장 완료: %s", target)

            # [SNAPSHOT-ENGINE] 신규 파일 최초 스냅샷
            #   - 기존 파일이 없던 경로이므로 '수정 직전 원본'이 존재하지 않는다.
            #   - 대신 저장 완료 직후의 상태를 최초 스냅샷으로 원장에 등록하여
            #     timeline.jsonl 에 파일 생성 시점이 정상 기록되도록 한다.
            if is_new_file:
                self._snapshot_after_create(target, note="initial creation")
        except Exception as e:
""",
)

# ----------------------------------------------------------------------
# 2) _snapshot_after_create 신규 메서드 추가
# ----------------------------------------------------------------------
patch(
    "add-snapshot-after-create",
    """    # ------------------------------------------------------------------
    # 3. 타임라인 조회
    # ------------------------------------------------------------------
    def get_timeline(self, file_path: Optional[str] = None,
""",
    """    # ------------------------------------------------------------------
    # 2-b. 신규 파일 최초 스냅샷 생성 (내부 훅)
    # ------------------------------------------------------------------
    def _snapshot_after_create(self, target: str,
                               note: str = "initial creation") -> Optional[Any]:
        \"\"\"
        신규 파일 생성 직후 최초 상태를 스냅샷으로 보존하고 원장에 기록한다.

        Args:
            target : 방금 생성된 파일 절대경로
            note   : 변경 요약 메모 (기본값 "initial creation")

        Returns:
            SnapshotRecord 또는 None (스킵/실패)

        원칙:
          - create_backup 은 디스크의 실제 파일을 읽으므로, 반드시 저장 완료 후 호출한다.
          - 스냅샷 실패는 파일 저장 결과를 절대 훼손하지 않는다 (예외 격리).
          - 신규 파일이므로 직전 스냅샷이 없어 내용 무변경 스킵에 걸리지 않는다.
        \"\"\"
        engine = self._ensure_snapshot_engine()
        if engine is None:
            return None

        try:
            if not os.path.isfile(target):
                logger.debug("[ProjectManager] 최초 스냅샷 생략(파일 없음): %s", target)
                return None

            rec = engine.create_backup(target, note=note)
            if rec is not None:
                logger.info(
                    "[ProjectManager] 최초 스냅샷 기록: %s (%s)",
                    rec.snapshot_id, os.path.basename(target),
                )
            return rec
        except Exception as e:
            logger.warning(
                "[ProjectManager] 최초 스냅샷 생성 실패(저장은 유지): %s (%s)", target, e
            )
            return None

    # ------------------------------------------------------------------
    # 3. 타임라인 조회
    # ------------------------------------------------------------------
    def get_timeline(self, file_path: Optional[str] = None,
""",
)


def main():
    with io.open(TARGET, "r", encoding="utf-8") as fh:
        text = fh.read()

    before_lines = text.count("\n") + 1
    applied = 0

    for name, search, replace in PATCHES:
        count = text.count(search)
        if count == 0:
            print("[SKIP] %s — SEARCH 미발견" % name)
            continue
        if count > 1:
            print("[ABORT] %s — SEARCH %d회 중복 (모호한 치환)" % (name, count))
            return 1
        text = text.replace(search, replace, 1)
        applied += 1
        print("[OK] %s" % name)

    if applied != len(PATCHES):
        print("미적용 패치 존재 — 파일 기록 중단 (applied=%d/%d)" % (applied, len(PATCHES)))
        return 1

    with io.open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)

    after_lines = text.count("\n") + 1
    print("PATCH_OK applied=%d/%d" % (applied, len(PATCHES)))
    print("LINES %d -> %d" % (before_lines, after_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
