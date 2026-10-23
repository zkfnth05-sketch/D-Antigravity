# -*- coding: utf-8 -*-
"""
_patch_apply_search_replace.py

project_manager.py 의 save_file_content 메서드 바로 아래에
'대용량 파일 정밀 부분 패치 엔진' apply_search_replace(self, file_path, patch_text) 를 삽입한다.

절차:
  1) 원본 읽기 (utf-8)
  2) 멱등성 검사 — 이미 apply_search_replace 가 있으면 중단
  3) 앵커(save_file_content 의 마지막 return 블록) 정확 1회 일치 확인
  4) 앵커 뒤에 신규 메서드 삽입
  5) 백업(.bak_apply_search_replace) → temp → os.replace 원자적 저장
  6) py_compile 검증 + 지표 실측 출력
"""

import io
import os
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "project_manager.py")
BACKUP = TARGET + ".bak_apply_search_replace"

# save_file_content 의 종료 지점(정확히 1회 등장해야 하는 앵커)
ANCHOR = '''        except Exception as e:
            logger.error("[ProjectManager] 저장 실패: %s (%s)", target, e)
            return {"success": False, "error": str(e), "path": target}
'''

NEW_METHOD = r'''
    # ------------------------------------------------------------------
    # 대용량 파일 정밀 부분 패치 엔진 (SEARCH / REPLACE)
    # ------------------------------------------------------------------
    # 패치 블록 형식 (Aider / Claude 계열 표준):
    #
    #   <<<<<<< SEARCH
    #   ...원본에서 찾을 코드...
    #   =======
    #   ...대체할 코드...
    #   >>>>>>> REPLACE
    #
    # 대용량 파일(수천 줄)을 통째로 재작성하지 않고, 국소 구간만 정밀 치환하여
    # 토큰 낭비와 회귀 위험을 동시에 줄이는 것이 목적이다.
    # ------------------------------------------------------------------
    PATCH_BLOCK_PATTERN = re.compile(
        r'<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE',
        re.DOTALL,
    )

    @staticmethod
    def _normalize_newlines(text: str) -> str:
        """CRLF / CR / LF 를 모두 LF 로 통일한다."""
        if not text:
            return ""
        return text.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _strip_all_whitespace(text: str) -> str:
        """모든 공백 문자(스페이스/탭/개행)를 제거한 비교용 문자열을 만든다."""
        if not text:
            return ""
        return re.sub(r'\s+', '', text)

    def _find_chunk_span(self, haystack: str, needle: str) -> Optional[tuple]:
        """
        haystack(원본) 안에서 needle(search_chunk)의 (start, end) 위치를 찾는다.

        치환 안정성 3단 폴백:
          (1) 정확 일치            — 원본 그대로 존재
          (2) 줄바꿈 정규화 일치    — CRLF/LF 혼용 차이 흡수
          (3) 공백 제거 일치        — 들여쓰기/공백 변형 흡수

        반환: (start, end) 튜플 또는 None
        """
        if not needle:
            return None

        # (1) 정확 일치
        idx = haystack.find(needle)
        if idx >= 0:
            return (idx, idx + len(needle))

        # (2) 줄바꿈 정규화 일치 — 원본과 needle 을 모두 LF 로 통일한 뒤 탐색.
        #     정규화 문자열의 인덱스는 원본과 1:1 대응하지 않으므로,
        #     원본을 LF 로 통일한 사본에서 위치를 찾고 그 사본을 기준으로 치환한다.
        norm_hay = self._normalize_newlines(haystack)
        norm_needle = self._normalize_newlines(needle)
        idx = norm_hay.find(norm_needle)
        if idx >= 0:
            return ("__NORMALIZED__", idx, idx + len(norm_needle))

        # (3) 공백 제거 일치 — 공백을 모두 제거한 뒤 부분 문자열 탐색.
        #     위치 역산이 불가능하므로 원본을 '라인 단위'로 재조립하며 매칭한다.
        target_sig = self._strip_all_whitespace(needle)
        if not target_sig:
            return None

        lines = haystack.splitlines(keepends=True)
        # 라인 누적 시그니처를 만들어, 연속 라인 구간의 시그니처가 target_sig 와
        # 일치하는 최초 구간을 찾는다.
        for start_line in range(len(lines)):
            acc = ""
            for end_line in range(start_line, len(lines)):
                acc += self._strip_all_whitespace(lines[end_line])
                if acc == target_sig:
                    start_off = sum(len(l) for l in lines[:start_line])
                    end_off = start_off + sum(len(l) for l in lines[start_line:end_line + 1])
                    return (start_off, end_off)
                if len(acc) > len(target_sig):
                    break

        return None

    def apply_search_replace(self, file_path: str, patch_text: str) -> Dict[str, Any]:
        """
        대용량 파일 정밀 부분 패치 엔진.

        patch_text 안의 모든 SEARCH/REPLACE 블록을 순서대로 적용한다.

        반환 계약:
          {
            "success": bool,          # 모든 블록이 적용되었는지 여부
            "applied": int,           # 실제 치환에 성공한 블록 수
            "total": int,             # 파싱된 전체 블록 수
            "failed": [ {...}, ... ], # 실패한 블록 상세 (index / reason / search 미리보기)
            "path": str,              # 실제 기록된 절대 경로
            "message": str,           # 사람이 읽는 요약
            "saved": bool,            # 디스크 기록 성공 여부
          }

        동작 원칙:
          - 블록이 하나도 파싱되지 않으면 파일을 절대 건드리지 않는다.
          - 적용된 블록이 0건이면 디스크에 쓰지 않는다(불필요한 mtime 변경 방지).
          - 일부 실패하더라도 성공한 블록은 저장하고, 실패 내역을 함께 반환한다.
        """
        result: Dict[str, Any] = {
            "success": False,
            "applied": 0,
            "total": 0,
            "failed": [],
            "path": "",
            "message": "",
            "saved": False,
        }

        # (0) 입력 방어
        if not file_path or not str(file_path).strip():
            result["message"] = "대상 파일 경로가 비어 있습니다."
            return result
        if not patch_text or not str(patch_text).strip():
            result["message"] = "패치 텍스트가 비어 있습니다."
            return result

        # (1) 대상 파일 읽기
        read_res = self.read_file_content(file_path)
        if not read_res.get("success"):
            result["path"] = read_res.get("path", "")
            result["message"] = "대상 파일 읽기 실패: %s" % read_res.get("error")
            return result

        original = read_res.get("content", "") or ""
        target_path = read_res.get("path", "")
        result["path"] = target_path

        # (2) 모든 SEARCH/REPLACE 블록 파싱
        blocks = self.PATCH_BLOCK_PATTERN.findall(patch_text)
        result["total"] = len(blocks)
        if not blocks:
            result["message"] = (
                "패치 블록을 찾지 못했습니다. "
                "'<<<<<<< SEARCH / ======= / >>>>>>> REPLACE' 형식을 확인하십시오."
            )
            return result

        # (3) 순차 치환
        current = original
        applied = 0
        failed: List[Dict[str, Any]] = []

        for idx, (search_chunk, replace_chunk) in enumerate(blocks, start=1):
            span = self._find_chunk_span(current, search_chunk)

            if span is None:
                failed.append({
                    "index": idx,
                    "reason": "SEARCH 구간을 원본에서 찾지 못했습니다.",
                    "search_preview": (search_chunk or "")[:200],
                })
                logger.warning(
                    "[ProjectManager] 패치 블록 %d/%d 실패 — SEARCH 미발견",
                    idx, len(blocks),
                )
                continue

            # (2) 줄바꿈 정규화 폴백으로 찾은 경우 — 정규화 사본 기준으로 치환
            if isinstance(span, tuple) and len(span) == 3 and span[0] == "__NORMALIZED__":
                _, s, e = span
                norm_current = self._normalize_newlines(current)
                norm_replacement = self._normalize_newlines(replace_chunk)
                current = norm_current[:s] + norm_replacement + norm_current[e:]
            else:
                s, e = span
                current = current[:s] + replace_chunk + current[e:]

            applied += 1
            logger.info(
                "[ProjectManager] 패치 블록 %d/%d 적용 완료 (%s)",
                idx, len(blocks), target_path,
            )

        result["applied"] = applied
        result["failed"] = failed

        # (4) 적용 0건이면 디스크 기록 생략 (불필요한 파일 변경 방지)
        if applied == 0:
            result["success"] = False
            result["message"] = (
                "적용된 블록이 없어 파일을 수정하지 않았습니다. (총 %d블록 전부 실패)" % len(blocks)
            )
            return result

        # (5) save_file_content 로 안전 저장
        save_res = self.save_file_content(target_path, current)
        result["saved"] = bool(save_res.get("success"))
        result["path"] = save_res.get("path", target_path)

        if not save_res.get("success"):
            result["success"] = False
            result["message"] = "치환은 성공했으나 디스크 저장 실패: %s" % save_res.get("error")
            return result

        # (6) 최종 결과 정리
        result["success"] = (len(failed) == 0)
        if failed:
            result["message"] = (
                "%d/%d 블록 적용 후 저장 완료 (실패 %d건)" % (applied, len(blocks), len(failed))
            )
        else:
            result["message"] = "%d/%d 블록 전량 적용 후 저장 완료" % (applied, len(blocks))

        logger.info(
            "[ProjectManager] apply_search_replace 완료: %s (%d/%d, 실패 %d)",
            result["path"], applied, len(blocks), len(failed),
        )
        return result
'''


def main() -> int:
    print("=" * 70)
    print("[1] 대상 파일 확인:", TARGET)
    if not os.path.isfile(TARGET):
        print("    ✗ 파일 없음 — 중단")
        return 1

    with io.open(TARGET, "r", encoding="utf-8") as f:
        src = f.read()
    before_lines = len(src.splitlines())
    print("    ✓ 원본 줄 수:", before_lines)

    print("[2] 멱등성 검사")
    if "def apply_search_replace" in src:
        print("    ⚠ 이미 apply_search_replace 가 존재 — 중단(중복 삽입 방지)")
        return 0
    print("    ✓ 미존재 확인")

    print("[3] 앵커 일치 검사")
    anchor_count = src.count(ANCHOR)
    print("    앵커 등장 횟수:", anchor_count)
    if anchor_count != 1:
        print("    ✗ 앵커가 정확히 1회가 아님 — 중단")
        return 1

    print("[4] 백업 생성:", BACKUP)
    shutil.copy2(TARGET, BACKUP)
    print("    ✓ 백업 완료")

    print("[5] 메서드 삽입")
    new_src = src.replace(ANCHOR, ANCHOR + NEW_METHOD, 1)
    after_lines = len(new_src.splitlines())
    print("    ✓ 삽입 완료: %d줄 → %d줄 (Δ+%d)" % (before_lines, after_lines, after_lines - before_lines))

    print("[6] 원자적 저장 (temp → os.replace)")
    tmp = TARGET + ".tmp_apply_sr"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_src)
    os.replace(tmp, TARGET)
    print("    ✓ 저장 완료")

    print("[7] py_compile 검증")
    try:
        py_compile.compile(TARGET, doraise=True)
        print("    ✓ COMPILE_OK")
    except py_compile.PyCompileError as e:
        print("    ✗ COMPILE_FAIL:", e)
        return 1

    print("[8] 지표 실측")
    with io.open(TARGET, "r", encoding="utf-8") as f:
        final = f.read()
    print("    최종 줄 수            :", len(final.splitlines()))
    print("    apply_search_replace  :", final.count("def apply_search_replace"))
    print("    PATCH_BLOCK_PATTERN   :", final.count("PATCH_BLOCK_PATTERN"))
    print("    _find_chunk_span      :", final.count("def _find_chunk_span"))
    print("    _normalize_newlines   :", final.count("def _normalize_newlines"))
    print("    _strip_all_whitespace :", final.count("def _strip_all_whitespace"))
    print("    import re 존재        :", ("import re" in final))
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
