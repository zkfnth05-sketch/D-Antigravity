# -*- coding: utf-8 -*-
"""snapshot_engine.py 에 RollbackOrchestrator + BackupDiffRollbackEngine 을 append 하는 패치 스크립트."""
import io
import os

TARGET = os.path.join("backend", "core", "snapshot_engine.py")

PART = r'''

# ======================================================================
# 7. RollbackOrchestrator — 스냅샷 복원 실행기
# ======================================================================
class RollbackOrchestrator:
    """
    스냅샷 복원(타임머신) 실행기.

    [복원 3경로]
      1) rollback_to_snapshot(snapshot_id)  : 특정 스냅샷으로 복원
      2) rollback_to_time(target_time)      : 특정 시각 이전 상태로 복원
      3) rollback_previous()                : 현재 파일의 직전 상태로 복원

    [안전장치]
      - 복원 직전 현재 파일 상태를 '[pre-rollback]' note 로 자동 스냅샷 보존.
        -> 복원 자체를 되돌릴 수 있는 안전망 확보.
      - 이 pre-rollback 레코드는 latest()/find_previous() 에서 기본 제외되어
        rollback_previous() 가 자기 자신을 복원하는 무한 루프를 원천 차단.
      - 복원 쓰기는 atomic_write_bytes 경유 (부분 기록으로 인한 파일 파손 방지).
      - 복원 후 sha256 재검증으로 무결성 확정.
    """

    def __init__(self, store: SnapshotStore, ledger: TimelineLedger,
                 diff_engine: DiffEngine) -> None:
        if not isinstance(store, SnapshotStore):
            raise TypeError("store 는 SnapshotStore 여야 합니다: %r" % (type(store),))
        if not isinstance(ledger, TimelineLedger):
            raise TypeError("ledger 는 TimelineLedger 여야 합니다: %r" % (type(ledger),))
        if not isinstance(diff_engine, DiffEngine):
            raise TypeError("diff_engine 은 DiffEngine 여야 합니다: %r" % (type(diff_engine),))

        self.store = store
        self.ledger = ledger
        self.diff_engine = diff_engine

    # -- 내부: 복원 직전 현재 상태 보존 ---------------------------------
    def _preserve_current(self, target_path: str, reason: str) -> Optional[SnapshotRecord]:
        """
        복원 직전 현재 파일 상태를 스냅샷으로 보존한다.

        파일이 존재하지 않으면 보존하지 않고 None 을 반환한다
        (신규 생성 파일을 롤백하는 정당한 시나리오).
        """
        abs_path = os.path.abspath(target_path)
        if not os.path.isfile(abs_path):
            logger.info("[Rollback] 복원 직전 보존 생략 (파일 없음): %s", abs_path)
            return None

        try:
            with open(abs_path, "rb") as fh:
                current = fh.read()
        except OSError as exc:
            raise SnapshotEngineError(
                "복원 직전 현재 파일 읽기 실패: %s (%s)" % (abs_path, exc)
            ) from exc

        note = "%s %s" % (PRE_ROLLBACK_NOTE_PREFIX, reason)
        rec = self.store.write_snapshot(abs_path, current, note=note)

        previous = self.ledger.latest(target_path=abs_path, include_pre_rollback=True)
        if previous is not None:
            rec.parent_snapshot_id = previous.snapshot_id
            diff = self.diff_engine.build(
                self.store.read_snapshot(previous),
                current,
                old_label=previous.snapshot_id,
                new_label=rec.snapshot_id,
                from_snapshot_id=previous.snapshot_id,
                to_snapshot_id=rec.snapshot_id,
            )
            if diff.changed:
                relpath, digest = self.store.write_diff(diff.unified_diff)
                rec.diff_relpath = relpath
                rec.diff_sha256 = digest

        self.ledger.append(rec)
        logger.info("[Rollback] 복원 직전 상태 보존 완료: %s", rec.snapshot_id)
        return rec

    # -- 내부: 실제 복원 실행 -------------------------------------------
    def _apply(self, rec: SnapshotRecord, reason: str) -> Dict[str, Any]:
        """
        스냅샷 레코드를 실제 파일로 복원한다.

        Returns:
            {"ok": True, "snapshot_id":..., "target_path":..., "size":...,
             "sha256":..., "preserved_snapshot_id":...}
        """
        abs_path = os.path.abspath(rec.target_path)

        # 1) 복원 직전 현재 상태 보존 (안전망)
        preserved = self._preserve_current(abs_path, reason)

        # 2) 스냅샷 바이트 로드 (해시 대조 포함)
        data = self.store.read_snapshot(rec)

        # 3) 원자적 복원
        ensure_disk_space(abs_path, len(data))
        written = atomic_write_bytes(abs_path, data, expected_sha256=rec.sha256)

        # 4) 복원 후 재검증
        actual = sha256_file(abs_path)
        if actual != rec.sha256:
            raise SnapshotIntegrityError(
                "복원 후 해시 불일치: %s (expected=%s actual=%s)"
                % (abs_path, rec.sha256, actual)
            )

        logger.info(
            "[Rollback] 복원 완료: %s -> %s (%d bytes, sha=%s)",
            rec.snapshot_id, abs_path, len(data), written[:12],
        )

        return {
            "ok": True,
            "snapshot_id": rec.snapshot_id,
            "target_path": abs_path,
            "size": len(data),
            "sha256": written,
            "preserved_snapshot_id": preserved.snapshot_id if preserved else "",
            "reason": reason,
        }

    # -- 공개 API -------------------------------------------------------
    def rollback_to_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        """
        특정 snapshot_id 로 복원한다.

        Raises:
            ValueError           : snapshot_id 누락
            SnapshotNotFoundError: 해당 id 가 원장에 없음
        """
        if not snapshot_id:
            raise ValueError("rollback_to_snapshot 은 snapshot_id 가 필수입니다.")

        for rec in self.ledger.load_all(reverse=True):
            if rec.snapshot_id == snapshot_id:
                return self._apply(rec, reason="rollback_to_snapshot(%s)" % snapshot_id)

        raise SnapshotNotFoundError("복원 대상 스냅샷을 찾을 수 없습니다: %s" % snapshot_id)

    def rollback_to_time(self, target_path: str, target_time: Any) -> Dict[str, Any]:
        """
        지정 시각과 같거나 그 이전 상태로 복원한다.

        Raises:
            ValueError           : 인자 누락 / 시각 파싱 불가
            SnapshotNotFoundError: 해당 시점 이전 스냅샷 없음
        """
        if not target_path:
            raise ValueError("rollback_to_time 은 target_path 가 필수입니다.")

        rec = self.ledger.find_at_or_before(target_path, target_time)
        if rec is None:
            raise SnapshotNotFoundError(
                "지정 시각 이전 스냅샷이 없습니다: %s @ %r" % (target_path, target_time)
            )

        return self._apply(rec, reason="rollback_to_time(%r)" % (target_time,))

    def rollback_previous(self, target_path: str) -> Dict[str, Any]:
        """
        현재 파일의 직전 스냅샷 상태로 복원한다.

        pre-rollback 보존 스냅샷은 기본 제외되므로,
        반복 호출 시에도 자기 자신을 복원하는 무한 루프가 발생하지 않는다.

        Raises:
            ValueError           : target_path 누락
            SnapshotNotFoundError: 직전 스냅샷 없음
        """
        if not target_path:
            raise ValueError("rollback_previous 는 target_path 가 필수입니다.")

        latest = self.ledger.latest(target_path=target_path)
        if latest is None:
            raise SnapshotNotFoundError(
                "복원할 스냅샷 이력이 없습니다: %s" % target_path
            )

        previous = self.ledger.find_previous(target_path, latest.snapshot_id)
        if previous is None:
            raise SnapshotNotFoundError(
                "직전 스냅샷이 없습니다 (최초 스냅샷): %s" % latest.snapshot_id
            )

        return self._apply(previous, reason="rollback_previous(%s)" % latest.snapshot_id)


# ======================================================================
# 8. BackupDiffRollbackEngine — 파사드 (ProjectManager 위임 대상)
# ======================================================================
class BackupDiffRollbackEngine:
    """
    스냅샷 / 원장 / Diff / 롤백 기능의 단일 진입점 파사드.

    ProjectManager 는 본 클래스 하나만 조립하면 되며,
    내부 컴포넌트(Store/Ledger/DiffEngine/Orchestrator)는 은닉된다.

    사용 예:
        engine = BackupDiffRollbackEngine(project_root)
        rec    = engine.create_backup("src/main.py", note="자동 저장")
        hist   = engine.get_timeline("src/main.py")
        diff   = engine.get_diff("src/main.py", rec.snapshot_id)
        result = engine.rollback_file("src/main.py", snapshot_id=rec.snapshot_id)
    """

    def __init__(self, project_root: str,
                 max_snapshots_per_file: int = DEFAULT_MAX_SNAPSHOTS_PER_FILE,
                 lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        if not project_root:
            raise ValueError("project_root 가 필요합니다.")

        self.project_root = os.path.abspath(project_root)
        self.base_dir = os.path.join(self.project_root, BACKUP_DIR_NAME)
        self.max_snapshots_per_file = int(max_snapshots_per_file)

        self.store = SnapshotStore(self.base_dir)
        self.ledger = TimelineLedger(self.base_dir, lock_timeout=lock_timeout)
        self.diff_engine = DiffEngine(DIFF_CONTEXT_LINES)
        self.orchestrator = RollbackOrchestrator(self.store, self.ledger, self.diff_engine)

        logger.info("[BackupEngine] 초기화 완료: %s", self.base_dir)

    # -- 경로 해석 ------------------------------------------------------
    def _resolve(self, target_path: str) -> str:
        """
        상대경로를 프로젝트 루트 기준 절대경로로 해석한다.

        Raises:
            ValueError: 경로 누락
        """
        if not target_path:
            raise ValueError("target_path 가 필요합니다.")

        raw = str(target_path).strip().strip('"').strip("'")
        if os.path.isabs(raw):
            return os.path.abspath(raw)
        return os.path.abspath(os.path.join(self.project_root, raw))

    # -- 1) 백업 생성 ---------------------------------------------------
    def create_backup(self, target_path: str, note: str = "",
                      force: bool = False) -> Optional[SnapshotRecord]:
        """
        대상 파일의 스냅샷을 생성하고 원장에 기록한다.

        Args:
            target_path : 대상 파일 (상대/절대)
            note        : 변경 요약 메모
            force       : True 면 내용 무변경이어도 스냅샷 생성

        Returns:
            SnapshotRecord, 또는 내용 무변경이고 force=False 이면 None

        Raises:
            SnapshotNotFoundError: 대상 파일 없음
            SnapshotEngineError  : 읽기/기록 실패
        """
        abs_path = self._resolve(target_path)

        if not os.path.isfile(abs_path):
            raise SnapshotNotFoundError("백업 대상 파일이 없습니다: %s" % abs_path)

        try:
            with open(abs_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise SnapshotEngineError(
                "백업 대상 읽기 실패: %s (%s)" % (abs_path, exc)
            ) from exc

        previous = self.ledger.latest(target_path=abs_path, include_pre_rollback=True)

        # 내용 무변경 스킵 (원장 비대화 방지)
        if previous is not None and not force:
            if previous.sha256 == sha256_bytes(data):
                logger.debug("[BackupEngine] 내용 무변경, 스냅샷 생략: %s", abs_path)
                return None

        rec = self.store.write_snapshot(abs_path, data, note=note)

        if previous is not None:
            rec.parent_snapshot_id = previous.snapshot_id
            try:
                old_data = self.store.read_snapshot(previous)
            except (SnapshotNotFoundError, SnapshotIntegrityError) as exc:
                logger.warning(
                    "[BackupEngine] 직전 스냅샷 로드 실패, diff 생략: %s (%s)",
                    previous.snapshot_id, exc,
                )
                old_data = None

            if old_data is not None:
                diff = self.diff_engine.build(
                    old_data,
                    data,
                    old_label=previous.snapshot_id,
                    new_label=rec.snapshot_id,
                    from_snapshot_id=previous.snapshot_id,
                    to_snapshot_id=rec.snapshot_id,
                )
                if diff.changed:
                    relpath, digest = self.store.write_diff(diff.unified_diff)
                    rec.diff_relpath = relpath
                    rec.diff_sha256 = digest

        self.ledger.append(rec)
        logger.info(
            "[BackupEngine] 스냅샷 생성: %s (%d bytes, +%s/-%s)",
            rec.snapshot_id, rec.size,
            "diff" if rec.diff_relpath else "0",
            "diff" if rec.diff_relpath else "0",
        )
        return rec

    # -- 2) 타임라인 조회 -----------------------------------------------
    def get_timeline(self, target_path: Optional[str] = None,
                     reverse: bool = True,
                     include_pre_rollback: bool = False) -> List[Dict[str, Any]]:
        """
        스냅샷 이력을 dict 목록으로 반환한다 (UI 직렬화 친화).

        Args:
            target_path          : None 이면 전체
            reverse              : True 면 최신순
            include_pre_rollback : 롤백 직전 보존 스냅샷 포함 여부
        """
        abs_path = self._resolve(target_path) if target_path else None
        records = self.ledger.load_all(target_path=abs_path, reverse=reverse)

        result: List[Dict[str, Any]] = []
        for rec in records:
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            item = rec.to_dict()
            item["is_pre_rollback"] = rec.is_pre_rollback
            result.append(item)
        return result

    # -- 3) Diff 조회 ---------------------------------------------------
    def get_diff(self, target_path: str, snapshot_id: str = "") -> Dict[str, Any]:
        """
        특정 스냅샷의 직전 대비 diff 를 반환한다.

        Args:
            target_path : 대상 파일
            snapshot_id : 조회할 스냅샷 id (빈 값이면 최신)

        Returns:
            {"snapshot_id":..., "from_snapshot_id":..., "changed":bool,
             "added_lines":int, "removed_lines":int, "diff":str}

        Raises:
            SnapshotNotFoundError: 스냅샷 또는 diff 없음
        """
        abs_path = self._resolve(target_path)

        if snapshot_id:
            target: Optional[SnapshotRecord] = None
            for rec in self.ledger.load_all(target_path=abs_path, reverse=True):
                if rec.snapshot_id == snapshot_id:
                    target = rec
                    break
            if target is None:
                raise SnapshotNotFoundError(
                    "스냅샷을 찾을 수 없습니다: %s (%s)" % (snapshot_id, abs_path)
                )
        else:
            target = self.ledger.latest(target_path=abs_path, include_pre_rollback=True)
            if target is None:
                raise SnapshotNotFoundError("스냅샷 이력이 없습니다: %s" % abs_path)

        if not target.diff_relpath:
            return {
                "snapshot_id": target.snapshot_id,
                "from_snapshot_id": target.parent_snapshot_id,
                "changed": False,
                "added_lines": 0,
                "removed_lines": 0,
                "diff": "",
            }

        text = self.store.read_diff(target.diff_relpath, target.diff_sha256)
        added, removed = DiffEngine.count_changes(text.splitlines())

        return {
            "snapshot_id": target.snapshot_id,
            "from_snapshot_id": target.parent_snapshot_id,
            "changed": True,
            "added_lines": added,
            "removed_lines": removed,
            "diff": text,
        }

    # -- 4) 롤백 --------------------------------------------------------
    def rollback_file(self, target_path: str, snapshot_id: str = "",
                      target_time: Any = None, previous: bool = False) -> Dict[str, Any]:
        """
        파일을 스냅샷 상태로 복원한다 (3경로 통합 진입점).

        우선순위: snapshot_id > target_time > previous

        Args:
            target_path : 대상 파일
            snapshot_id : 특정 스냅샷 id 로 복원
            target_time : 지정 시각 이전 상태로 복원
            previous    : 직전 상태로 복원

        Returns:
            복원 결과 dict

        Raises:
            ValueError: 복원 방식 미지정
        """
        abs_path = self._resolve(target_path)

        if snapshot_id:
            return self.orchestrator.rollback_to_snapshot(snapshot_id)
        if target_time is not None:
            return self.orchestrator.rollback_to_time(abs_path, target_time)
        if previous:
            return self.orchestrator.rollback_previous(abs_path)

        raise ValueError(
            "복원 방식이 지정되지 않았습니다: snapshot_id / target_time / previous 중 하나 필요"
        )

    # -- 5) 무결성 검증 -------------------------------------------------
    def verify(self, target_path: Optional[str] = None) -> Dict[str, Any]:
        """
        원장 및 스냅샷 객체의 무결성을 전수 검증한다.

        Returns:
            {"ledger": {...}, "objects_checked": int, "objects_missing": [...],
             "objects_corrupt": [...], "ok": bool}

        Raises:
            LedgerCorruptError: 원장 자체가 손상된 경우
        """
        report = self.ledger.validate()

        abs_path = self._resolve(target_path) if target_path else None
        records = self.ledger.load_all(target_path=abs_path)

        missing: List[str] = []
        corrupt: List[str] = []
        checked = 0

        for rec in records:
            checked += 1
            try:
                self.store.read_snapshot(rec)
            except SnapshotNotFoundError as exc:
                missing.append("%s: %s" % (rec.snapshot_id, exc))
            except SnapshotIntegrityError as exc:
                corrupt.append("%s: %s" % (rec.snapshot_id, exc))

        ok = not missing and not corrupt

        if not ok:
            logger.error(
                "[BackupEngine] 무결성 검증 실패: 누락=%d 손상=%d",
                len(missing), len(corrupt),
            )

        return {
            "ledger": report,
            "objects_checked": checked,
            "objects_missing": missing,
            "objects_corrupt": corrupt,
            "ok": ok,
        }

    # -- 6) 좀비 임시파일 회수 ------------------------------------------
    def sweep_zombie_tmp(self, max_age: float = ZOMBIE_TMP_MAX_AGE) -> List[str]:
        """고아 임시파일을 회수한다."""
        return self.store.sweep_zombie_tmp(max_age=max_age)

    # -- 7) 보존 정책 (오래된 스냅샷 정리) ------------------------------
    def prune(self, target_path: str,
              keep: Optional[int] = None) -> Dict[str, Any]:
        """
        파일당 스냅샷 보존 상한을 초과한 오래된 객체를 정리한다.

        원장(append-only)은 절대 수정하지 않으며, 물리 객체 파일만 회수한다.
        (원장 라인은 이력 증거로 영구 보존)

        Args:
            target_path : 대상 파일
            keep        : 보존 개수 (기본 max_snapshots_per_file)

        Returns:
            {"kept": int, "removed_objects": int, "removed_diffs": int}
        """
        abs_path = self._resolve(target_path)
        limit = int(keep) if keep is not None else self.max_snapshots_per_file
        if limit < 1:
            raise ValueError("keep 은 1 이상이어야 합니다: %r" % (keep,))

        records = self.ledger.load_all(target_path=abs_path, reverse=True)
        if len(records) <= limit:
            return {"kept": len(records), "removed_objects": 0, "removed_diffs": 0}

        keep_set = {rec.snapshot_id for rec in records[:limit]}
        keep_sha = {rec.sha256 for rec in records[:limit]}
        keep_diff = {rec.diff_sha256 for rec in records[:limit] if rec.diff_sha256}

        removed_objects = 0
        removed_diffs = 0

        for rec in records[limit:]:
            if rec.sha256 and rec.sha256 not in keep_sha:
                obj_path = self.store.abspath(rec.snapshot_relpath)
                if os.path.isfile(obj_path):
                    try:
                        os.remove(obj_path)
                        removed_objects += 1
                    except OSError as exc:
                        logger.warning(
                            "[BackupEngine] 스냅샷 객체 삭제 실패: %s (%s)", obj_path, exc
                        )
            if rec.diff_sha256 and rec.diff_sha256 not in keep_diff:
                diff_path = self.store.abspath(rec.diff_relpath)
                if os.path.isfile(diff_path):
                    try:
                        os.remove(diff_path)
                        removed_diffs += 1
                    except OSError as exc:
                        logger.warning(
                            "[BackupEngine] diff 객체 삭제 실패: %s (%s)", diff_path, exc
                        )

        logger.info(
            "[BackupEngine] 보존 정책 적용: kept=%d removed_objects=%d removed_diffs=%d",
            limit, removed_objects, removed_diffs,
        )
        return {
            "kept": limit,
            "removed_objects": removed_objects,
            "removed_diffs": removed_diffs,
        }
'''

with io.open(TARGET, "r", encoding="utf-8") as fh:
    original = fh.read()

if "class RollbackOrchestrator" in original or "class BackupDiffRollbackEngine" in original:
    raise SystemExit("이미 RollbackOrchestrator/BackupDiffRollbackEngine 이 존재합니다. 중복 append 를 중단합니다.")

with io.open(TARGET, "a", encoding="utf-8", newline="") as fh:
    fh.write(PART)

with io.open(TARGET, "r", encoding="utf-8") as fh:
    final = fh.read()

print("APPEND_OK")
print("LINES", final.count("\n"))
print("HAS_RollbackOrchestrator", "class RollbackOrchestrator" in final)
print("HAS_BackupDiffRollbackEngine", "class BackupDiffRollbackEngine" in final)
