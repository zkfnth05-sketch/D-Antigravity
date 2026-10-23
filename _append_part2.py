# -*- coding: utf-8 -*-
"""snapshot_engine.py 에 DiffEngine + TimelineLedger 를 안전하게 append 하는 패치 스크립트."""
import io
import os

TARGET = os.path.join("backend", "core", "snapshot_engine.py")

PART = r'''

# ======================================================================
# 5. DiffEngine — unified diff 생성 및 +/- 라인 집계
# ======================================================================
class DiffEngine:
    """
    두 스냅샷(바이트) 사이의 unified diff 를 생성하고 변경량을 집계한다.

    [개행 정규화]
      CRLF(\r\n) 와 LF(\n) 차이만 있는 파일은 '변경 없음'으로 판정한다.
      Windows IDE 에서 저장할 때마다 전체 라인이 변경으로 잡히는 노이즈를 차단한다.
      단, CR 단독(\r)은 정규화 대상이 아니며 실제 변경으로 취급한다.

    [라인 집계 정확성]
      difflib.unified_diff 는 '---' / '+++' 파일 헤더 라인을 포함하므로,
      헤더를 제외한 뒤 '+' / '-' 접두사 라인만 집계한다.
      '+++' / '---' 헤더를 추가/삭제 라인으로 오집계하는 버그를 원천 차단한다.
    """

    def __init__(self, context_lines: int = DIFF_CONTEXT_LINES) -> None:
        if context_lines < 0:
            raise ValueError("context_lines 는 0 이상이어야 합니다: %r" % (context_lines,))
        self.context_lines = int(context_lines)

    # -- 개행 정규화 ----------------------------------------------------
    @staticmethod
    def normalize_newlines(data: bytes) -> bytes:
        """CRLF -> LF 정규화 (CR 단독은 보존)."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("normalize_newlines 는 bytes 를 요구합니다: %r" % (type(data),))
        return bytes(data).replace(b"\r\n", b"\n")

    @staticmethod
    def decode_text(data: bytes) -> str:
        """
        바이트를 텍스트로 디코딩한다.

        UTF-8 우선, 실패 시 cp949(한국어 Windows 기본) 폴백.
        두 인코딩 모두 실패하면 SnapshotEngineError 로 승격한다.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("decode_text 는 bytes 를 요구합니다: %r" % (type(data),))

        payload = bytes(data)
        for encoding in ("utf-8", "cp949"):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError as exc:
                logger.debug("[DiffEngine] %s 디코딩 실패, 다음 인코딩 시도: %s", encoding, exc)

        raise SnapshotEngineError(
            "텍스트 디코딩 실패 (utf-8/cp949 모두 실패, %d bytes)" % len(payload)
        )

    def _to_lines(self, data: bytes) -> List[str]:
        """
        바이트를 개행 정규화된 라인 리스트로 변환한다.

        splitlines() 를 사용하여 말미 개행 유무 차이를 흡수한다.
        (파일 끝 개행 추가/제거만으로 전체 라인이 변경되는 노이즈 차단)
        """
        normalized = self.normalize_newlines(data)
        text = self.decode_text(normalized)
        return text.splitlines()

    # -- 라인 집계 ------------------------------------------------------
    @staticmethod
    def count_changes(diff_lines: Iterable[str]) -> Tuple[int, int]:
        """
        unified diff 라인 목록에서 (added, removed) 를 정확히 집계한다.

        규칙:
          - '+++' / '---' 로 시작하는 파일 헤더 라인은 집계에서 제외
          - '+' 로 시작하는 라인 = 추가
          - '-' 로 시작하는 라인 = 삭제
          - ' ' / '@@' / '\\' 로 시작하는 라인 = 무시

        Returns:
            (added_lines, removed_lines)
        """
        added = 0
        removed = 0

        for line in diff_lines:
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1

        return added, removed

    # -- diff 생성 ------------------------------------------------------
    def build(self, old_data: bytes, new_data: bytes,
              old_label: str = "before", new_label: str = "after",
              from_snapshot_id: str = "", to_snapshot_id: str = "") -> DiffRecord:
        """
        두 스냅샷 바이트로 DiffRecord 를 생성한다.

        변경이 없으면 changed=False, unified_diff="" 를 반환한다.
        (개행 스타일 차이만 있는 경우도 변경 없음으로 판정)

        Args:
            old_data         : 이전 스냅샷 바이트
            new_data         : 이후 스냅샷 바이트
            old_label        : diff 헤더에 표기할 이전 파일 라벨
            new_label        : diff 헤더에 표기할 이후 파일 라벨
            from_snapshot_id : 이전 스냅샷 id
            to_snapshot_id   : 이후 스냅샷 id

        Returns:
            DiffRecord
        """
        if not isinstance(old_data, (bytes, bytearray)):
            raise TypeError("build 의 old_data 는 bytes 여야 합니다: %r" % (type(old_data),))
        if not isinstance(new_data, (bytes, bytearray)):
            raise TypeError("build 의 new_data 는 bytes 여야 합니다: %r" % (type(new_data),))

        old_lines = self._to_lines(bytes(old_data))
        new_lines = self._to_lines(bytes(new_data))

        # 정규화 후 완전 동일 -> 변경 없음 (diff 계산 자체를 생략)
        if old_lines == new_lines:
            return DiffRecord(
                from_snapshot_id=from_snapshot_id,
                to_snapshot_id=to_snapshot_id,
                created_at=iso_now(),
                added_lines=0,
                removed_lines=0,
                changed=False,
                unified_diff="",
            )

        diff_iter = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=old_label,
            tofile=new_label,
            n=self.context_lines,
            lineterm="",
        )
        diff_lines = list(diff_iter)

        added, removed = self.count_changes(diff_lines)
        unified = "\n".join(diff_lines)

        return DiffRecord(
            from_snapshot_id=from_snapshot_id,
            to_snapshot_id=to_snapshot_id,
            created_at=iso_now(),
            added_lines=added,
            removed_lines=removed,
            changed=bool(diff_lines),
            unified_diff=unified,
        )

    def build_from_text(self, old_text: str, new_text: str,
                        old_label: str = "before", new_label: str = "after",
                        from_snapshot_id: str = "", to_snapshot_id: str = "") -> DiffRecord:
        """텍스트 두 개로 DiffRecord 를 생성한다 (편의 API)."""
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise TypeError("build_from_text 는 str 두 개를 요구합니다.")
        return self.build(
            old_text.encode("utf-8"),
            new_text.encode("utf-8"),
            old_label=old_label,
            new_label=new_label,
            from_snapshot_id=from_snapshot_id,
            to_snapshot_id=to_snapshot_id,
        )


# ======================================================================
# 6. TimelineLedger — append-only 원장 (timeline.jsonl)
# ======================================================================
class TimelineLedger:
    """
    스냅샷 이력의 append-only 원장.

    파일: <base_dir>/timeline.jsonl  (1줄 = 1 SnapshotRecord JSON)
    잠금: <base_dir>/.ledger.lock    (LedgerLock)

    [append-only 원칙]
      기존 라인은 절대 수정/삭제하지 않는다. 추가는 항상 파일 끝에만 수행한다.
      이로써 크래시 시에도 부분 기록된 마지막 줄만 손상되고, 그 이전 이력은
      온전히 보존된다.

    [손상 라인 정책]
      - load_all()      : 손상 라인은 경고 로깅 후 건너뛴다 (가용성 우선)
      - validate()      : 손상 라인을 전수 수집하여 LedgerCorruptError 로 승격
                          (무결성 감사 우선)
    """

    def __init__(self, base_dir: str, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.base_dir = os.path.abspath(base_dir)
        self.ledger_path = os.path.join(self.base_dir, LEDGER_FILE_NAME)
        self.lock_path = os.path.join(self.base_dir, LOCK_FILE_NAME)
        self.lock_timeout = float(lock_timeout)
        os.makedirs(self.base_dir, exist_ok=True)

    # -- 잠금 -----------------------------------------------------------
    def _lock(self) -> LedgerLock:
        return LedgerLock(self.lock_path, timeout=self.lock_timeout)

    # -- append ---------------------------------------------------------
    def append(self, rec: SnapshotRecord) -> SnapshotRecord:
        """
        레코드 1건을 원장 끝에 추가한다.

        절차:
          1) 디스크 여유 공간 검사 (DiskSpaceError)
          2) LedgerLock 획득 (LedgerLockTimeout)
          3) JSON 1줄 직렬화 후 개행 추가
          4) 파일 끝에 append + flush + fsync (부분 기록 최소화)

        Raises:
            TypeError        : rec 이 SnapshotRecord 가 아닌 경우
            DiskSpaceError   : 디스크 여유 부족
            LedgerLockTimeout: 잠금 획득 실패
            SnapshotEngineError: 파일 기록 실패
        """
        if not isinstance(rec, SnapshotRecord):
            raise TypeError("append 는 SnapshotRecord 를 요구합니다: %r" % (type(rec),))

        line = rec.to_json_line()
        if "\n" in line or "\r" in line:
            raise LedgerCorruptError(
                "직렬화된 레코드에 개행이 포함되어 원장 1줄 규약을 위반합니다: %s"
                % rec.snapshot_id
            )

        payload = (line + "\n").encode("utf-8")
        ensure_disk_space(self.base_dir, len(payload))

        with self._lock():
            try:
                with open(self.ledger_path, "ab") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                raise SnapshotEngineError(
                    "원장 append 실패: %s (%s)" % (self.ledger_path, exc)
                ) from exc

        logger.debug("[TimelineLedger] append 완료: %s (%s)", rec.snapshot_id, rec.target_path)
        return rec

    # -- 원시 라인 읽기 -------------------------------------------------
    def _read_raw_lines(self) -> List[Tuple[int, str]]:
        """
        원장을 (줄번호, 원문) 목록으로 반환한다. 빈 줄은 제외.

        줄번호는 1부터 시작하며, 손상 라인 보고에 사용된다.
        """
        if not os.path.isfile(self.ledger_path):
            return []

        try:
            with open(self.ledger_path, "r", encoding="utf-8", newline="") as fh:
                raw = fh.read()
        except OSError as exc:
            raise SnapshotEngineError(
                "원장 읽기 실패: %s (%s)" % (self.ledger_path, exc)
            ) from exc
        except UnicodeDecodeError as exc:
            raise LedgerCorruptError(
                "원장 UTF-8 디코딩 실패: %s (%s)" % (self.ledger_path, exc)
            ) from exc

        result: List[Tuple[int, str]] = []
        for idx, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            result.append((idx, line))
        return result

    # -- load_all -------------------------------------------------------
    def load_all(self, target_path: Optional[str] = None,
                 reverse: bool = False) -> List[SnapshotRecord]:
        """
        전체 또는 특정 파일의 스냅샷 이력을 로드한다.

        Args:
            target_path : None 이면 전체, 지정 시 해당 파일 이력만
            reverse     : True 면 최신순(역순), False 면 과거순(정순)

        Returns:
            SnapshotRecord 목록

        Note:
            손상 라인은 경고 로깅 후 건너뛴다. 엄격 감사는 validate() 사용.
        """
        wanted = norm_path(target_path) if target_path else ""
        records: List[SnapshotRecord] = []

        for lineno, line in self._read_raw_lines():
            try:
                data = json.loads(line)
                rec = SnapshotRecord.from_dict(data)
            except (json.JSONDecodeError, LedgerCorruptError) as exc:
                logger.warning(
                    "[TimelineLedger] 손상 라인 건너뜀 (line %d): %s", lineno, exc
                )
                continue

            if wanted and norm_path(rec.target_path) != wanted:
                continue
            records.append(rec)

        if reverse:
            records.reverse()
        return records

    # -- validate -------------------------------------------------------
    def validate(self) -> Dict[str, Any]:
        """
        원장 전수 감사.

        검사 항목:
          1) JSON 파싱 가능 여부
          2) 필수 필드 존재 여부 (SnapshotRecord.from_dict)
          3) snapshot_id 중복 여부
          4) snapshot_id / target_path / created_at 비어있음 여부
          5) created_at 파싱 가능 여부
          6) parent_snapshot_id 체인 무결성 (동일 파일 내 존재 여부)

        Returns:
            {
              "total": int, "valid": int, "corrupt": int,
              "duplicates": [...], "chain_breaks": [...], "errors": [...]
            }

        Raises:
            LedgerCorruptError: 손상/중복/체인 단절이 1건이라도 발견된 경우
        """
        raw_lines = self._read_raw_lines()

        errors: List[str] = []
        duplicates: List[str] = []
        chain_breaks: List[str] = []

        seen_ids: Dict[str, int] = {}
        records: List[SnapshotRecord] = []
        corrupt = 0

        for lineno, line in raw_lines:
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                corrupt += 1
                errors.append("line %d: JSON 파싱 실패 (%s)" % (lineno, exc))
                continue

            try:
                rec = SnapshotRecord.from_dict(data)
            except LedgerCorruptError as exc:
                corrupt += 1
                errors.append("line %d: 레코드 변환 실패 (%s)" % (lineno, exc))
                continue

            if not rec.snapshot_id:
                corrupt += 1
                errors.append("line %d: snapshot_id 가 비어 있습니다" % lineno)
                continue
            if not rec.target_path:
                corrupt += 1
                errors.append("line %d: target_path 가 비어 있습니다 (%s)" % (lineno, rec.snapshot_id))
                continue
            if not rec.created_at:
                corrupt += 1
                errors.append("line %d: created_at 이 비어 있습니다 (%s)" % (lineno, rec.snapshot_id))
                continue
            if rec.created_dt is None:
                corrupt += 1
                errors.append(
                    "line %d: created_at 파싱 불가 (%s, %r)"
                    % (lineno, rec.snapshot_id, rec.created_at)
                )
                continue

            if rec.snapshot_id in seen_ids:
                duplicates.append(
                    "snapshot_id 중복: %s (line %d, 최초 line %d)"
                    % (rec.snapshot_id, lineno, seen_ids[rec.snapshot_id])
                )
            else:
                seen_ids[rec.snapshot_id] = lineno

            records.append(rec)

        # parent 체인 무결성: 동일 파일 내에서 parent 가 실재해야 한다
        by_target: Dict[str, set] = {}
        for rec in records:
            by_target.setdefault(norm_path(rec.target_path), set()).add(rec.snapshot_id)

        for rec in records:
            if not rec.parent_snapshot_id:
                continue
            siblings = by_target.get(norm_path(rec.target_path), set())
            if rec.parent_snapshot_id not in siblings:
                chain_breaks.append(
                    "체인 단절: %s 의 parent=%s 가 동일 파일 이력에 없음"
                    % (rec.snapshot_id, rec.parent_snapshot_id)
                )

        report = {
            "total": len(raw_lines),
            "valid": len(records),
            "corrupt": corrupt,
            "duplicates": duplicates,
            "chain_breaks": chain_breaks,
            "errors": errors,
        }

        if corrupt or duplicates or chain_breaks:
            raise LedgerCorruptError(
                "원장 무결성 감사 실패: total=%d valid=%d corrupt=%d 중복=%d 체인단절=%d | %s"
                % (
                    report["total"], report["valid"], corrupt,
                    len(duplicates), len(chain_breaks),
                    "; ".join((errors + duplicates + chain_breaks)[:5]),
                )
            )

        logger.info("[TimelineLedger] 원장 감사 통과: %d 건", report["valid"])
        return report

    # -- latest ---------------------------------------------------------
    def latest(self, target_path: Optional[str] = None,
               include_pre_rollback: bool = False) -> Optional[SnapshotRecord]:
        """
        최신 스냅샷을 조회한다.

        Args:
            target_path          : None 이면 전체 최신, 지정 시 해당 파일 최신
            include_pre_rollback : False 면 롤백 직전 자동 보존 스냅샷을 제외
                                   (rollback_previous 무한 루프 방지)

        Returns:
            SnapshotRecord 또는 None (이력 없음)
        """
        records = self.load_all(target_path=target_path, reverse=True)

        for rec in records:
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            return rec

        return None

    # -- find_at_or_before ----------------------------------------------
    def find_at_or_before(self, target_path: str, target_time: Any,
                          include_pre_rollback: bool = False) -> Optional[SnapshotRecord]:
        """
        지정 시각과 같거나 그 이전의 가장 최근 스냅샷을 조회한다.

        Args:
            target_path          : 대상 파일 경로 (필수)
            target_time          : datetime / epoch / 문자열 시각
            include_pre_rollback : 롤백 직전 스냅샷 포함 여부

        Returns:
            SnapshotRecord 또는 None

        Raises:
            ValueError: target_time 파싱 불가
        """
        if not target_path:
            raise ValueError("find_at_or_before 는 target_path 가 필수입니다.")

        when = parse_when(target_time)
        if when is None:
            raise ValueError("target_time 을 해석할 수 없습니다: %r" % (target_time,))

        records = self.load_all(target_path=target_path, reverse=True)

        for rec in records:
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            created = rec.created_dt
            if created is None:
                logger.warning(
                    "[TimelineLedger] created_at 파싱 불가 레코드 건너뜀: %s (%r)",
                    rec.snapshot_id, rec.created_at,
                )
                continue
            if created <= when:
                return rec

        return None

    # -- find_previous --------------------------------------------------
    def find_previous(self, target_path: str, snapshot_id: str,
                      include_pre_rollback: bool = False) -> Optional[SnapshotRecord]:
        """
        지정 스냅샷의 직전 스냅샷을 조회한다.

        parent_snapshot_id 체인을 우선 신뢰하되, 체인이 비어 있거나
        참조 대상이 없으면 시간순 인접 레코드로 폴백한다.

        Args:
            target_path          : 대상 파일 경로 (필수)
            snapshot_id          : 기준 스냅샷 id (필수)
            include_pre_rollback : 롤백 직전 스냅샷 포함 여부

        Returns:
            직전 SnapshotRecord 또는 None (기준이 최초이거나 미발견)

        Raises:
            ValueError           : 인자 누락
            SnapshotNotFoundError: 기준 snapshot_id 가 이력에 없음
        """
        if not target_path:
            raise ValueError("find_previous 는 target_path 가 필수입니다.")
        if not snapshot_id:
            raise ValueError("find_previous 는 snapshot_id 가 필수입니다.")

        records = self.load_all(target_path=target_path, reverse=False)

        index = -1
        for i, rec in enumerate(records):
            if rec.snapshot_id == snapshot_id:
                index = i
                break

        if index < 0:
            raise SnapshotNotFoundError(
                "기준 스냅샷을 찾을 수 없습니다: %s (%s)" % (snapshot_id, target_path)
            )

        current = records[index]

        # 1순위: parent 체인
        if current.parent_snapshot_id:
            for rec in records:
                if rec.snapshot_id == current.parent_snapshot_id:
                    if not include_pre_rollback and rec.is_pre_rollback:
                        break
                    return rec
            logger.warning(
                "[TimelineLedger] parent 체인 참조 실패, 시간순 폴백: %s -> %s",
                current.snapshot_id, current.parent_snapshot_id,
            )

        # 2순위: 시간순 인접 레코드 폴백
        for i in range(index - 1, -1, -1):
            rec = records[i]
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            return rec

        return None

    # -- 유틸 -----------------------------------------------------------
    def count(self, target_path: Optional[str] = None) -> int:
        """원장 레코드 수를 반환한다."""
        return len(self.load_all(target_path=target_path))

    def exists(self) -> bool:
        """원장 파일 존재 여부."""
        return os.path.isfile(self.ledger_path)
'''

with io.open(TARGET, "r", encoding="utf-8") as fh:
    original = fh.read()

if "class DiffEngine" in original or "class TimelineLedger" in original:
    raise SystemExit("이미 DiffEngine/TimelineLedger 가 존재합니다. 중복 append 를 중단합니다.")

with io.open(TARGET, "a", encoding="utf-8", newline="") as fh:
    fh.write(PART)

with io.open(TARGET, "r", encoding="utf-8") as fh:
    final = fh.read()

print("APPEND_OK")
print("LINES", final.count("\n"))
print("HAS_DiffEngine", "class DiffEngine" in final)
print("HAS_TimelineLedger", "class TimelineLedger" in final)
