# -*- coding: utf-8 -*-
"""
_patch_mcp_force_sync.py

mcp_server.py 의 _tool_autonomous_codegen 에
'SEARCH/REPLACE 최우선 정밀 패치' 게이트를 배선한다.

배선 순서 (최우선순위):
  1) output 에 '<<<<<<< SEARCH' 가 포함되어 있으면
     → self.pm.apply_search_replace(full_p, output) 를 최우선 호출 (부분 정밀 패치)
  2) 그 외에는 기존 코드블록 전체 저장(force sync) 로직 유지

절차: 멱등성 → 앵커 1회 확인 → 백업 → 원자 저장 → py_compile → 지표 실측
"""

import io
import os
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "mcp_server.py")
BACKUP = TARGET + ".bak_mcp_force_sync"

ANCHOR = '''        force_sync_banner = ""
        after_check = sha256_of_file(full_p)
        if full_p and (not after_check["exists"] or after_check["hash"] == before_hash):
            code_blocks = re.findall(r'```(?:python)?\\s*([\\s\\S]*?)```', output or "")
'''

NEW_BLOCK = '''        force_sync_banner = ""
        after_check = sha256_of_file(full_p)

        # ------------------------------------------------------------------
        # [최우선] SEARCH/REPLACE 정밀 부분 패치 게이트
        # 응답에 '<<<<<<< SEARCH' 블록이 있으면 대용량 파일 전체 재작성 대신
        # apply_search_replace 로 국소 구간만 정밀 치환한다. (토큰 절감 + 회귀 방지)
        # ------------------------------------------------------------------
        if full_p and output and "<<<<<<< SEARCH" in output:
            try:
                asr_res = self.pm.apply_search_replace(full_p, output)
            except Exception as e:
                asr_res = {"success": False, "applied": 0, "total": 0,
                           "saved": False, "message": "apply_search_replace 예외: %s" % e}

            applied_n = asr_res.get("applied", 0)
            total_n = asr_res.get("total", 0)
            saved_ok = asr_res.get("saved", False)

            if applied_n > 0 and saved_ok:
                self.log("[MCP SEARCH/REPLACE] 정밀 부분 패치 완료 -> %s (%d/%d 블록)"
                         % (full_p, applied_n, total_n))
                force_sync_banner = (
                    "\\n\\n🎯 [MCP SEARCH/REPLACE 정밀 부분 패치]\\n"
                    "`%s` 에 SEARCH/REPLACE 블록 %d/%d 건을 국소 정밀 치환하여 저장했습니다. "
                    "(대용량 파일 전체 재작성 회피)" % (full_p, applied_n, total_n)
                )
                if asr_res.get("failed"):
                    force_sync_banner += "\\n- 실패 블록 %d건: %s" % (
                        len(asr_res["failed"]),
                        "; ".join(f.get("reason", "") for f in asr_res["failed"][:3])
                    )
            else:
                self.log("[MCP SEARCH/REPLACE] 정밀 패치 미적용 -> %s (%s)"
                         % (full_p, asr_res.get("message", "")))
                force_sync_banner = (
                    "\\n\\n⚠️ [MCP SEARCH/REPLACE 정밀 부분 패치 실패]\\n"
                    "`%s` — %s (적용 %d/%d 블록)"
                    % (full_p, asr_res.get("message", "알 수 없는 사유"), applied_n, total_n)
                )

            # 정밀 패치를 시도했으므로 코드블록 전체 덮어쓰기 게이트는 건너뛴다.
            after_check = sha256_of_file(full_p)

        elif full_p and (not after_check["exists"] or after_check["hash"] == before_hash):
            code_blocks = re.findall(r'```(?:python)?\\s*([\\s\\S]*?)```', output or "")
'''


def main() -> int:
    print("=" * 72)
    print("[1] 대상:", TARGET)
    if not os.path.isfile(TARGET):
        print("    ✗ 파일 없음")
        return 1

    with io.open(TARGET, "r", encoding="utf-8") as f:
        src = f.read()
    before_lines = len(src.splitlines())
    print("    ✓ 원본 줄 수:", before_lines)

    print("[2] 멱등성 검사")
    if "apply_search_replace(full_p, output)" in src:
        print("    ⚠ 이미 배선됨 — 중단")
        return 0
    print("    ✓ 미배선 확인")

    print("[3] 앵커 일치 검사")
    cnt = src.count(ANCHOR)
    print("    앵커 등장 횟수:", cnt)
    if cnt != 1:
        print("    ✗ 앵커가 정확히 1회가 아님 — 중단")
        return 1

    print("[4] 백업:", BACKUP)
    shutil.copy2(TARGET, BACKUP)
    print("    ✓ 백업 완료")

    print("[5] SEARCH/REPLACE 최우선 게이트 삽입")
    new_src = src.replace(ANCHOR, NEW_BLOCK, 1)
    after_lines = len(new_src.splitlines())
    print("    ✓ %d줄 → %d줄 (Δ+%d)" % (before_lines, after_lines, after_lines - before_lines))

    print("[6] 원자적 저장")
    tmp = TARGET + ".tmp_mcp_fs"
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
    print("    최종 줄 수                       :", len(final.splitlines()))
    print("    apply_search_replace 호출        :", final.count("apply_search_replace(full_p, output)"))
    print("    '<<<<<<< SEARCH' 검사            :", final.count('"<<<<<<< SEARCH" in output'))
    print("    SEARCH/REPLACE 배너              :", final.count("MCP SEARCH/REPLACE"))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
