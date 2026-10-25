#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
 Git Deployment Guardian - 대표님 '깃에저장' 공식 명령 집행기
================================================================================
 사용법:
     python git_commit_guardian.py            # 기본: add + commit (push 안 함)
     python git_commit_guardian.py --push     # 커밋 후 원격 푸시까지 수행
     python git_commit_guardian.py --dry-run  # 실제 반영 없이 계획만 출력

 정책:
   - DeepSeek_Antigravity_IDE : 전체 변경분 add + commit
   - aider-main               : 거버넌스 파일 add + commit
                                (.bak / _patch_* / *.log / 세션파일 제외)
   - 원격 푸시는 --push 명시 시에만 수행 (기본 대기)
================================================================================
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ------------------------------------------------------------------------------
# 0. 상수
# ------------------------------------------------------------------------------
MAIN_REPO_NAME = "DeepSeek_Antigravity_IDE"
AIDER_REPO_NAME = "aider-main"

MAIN_COMMIT_MSG = (
    "feat(council): 6-member joint council handoff recovery & "
    "standalone launcher launch_ide.py"
)
AIDER_COMMIT_MSG = "feat(governance): council runtime & enforcer hooks sync"

# aider-main 에서 커밋에서 제외할 임시/백업 산출물 패턴
AIDER_EXCLUDE_PATTERNS = [
    "*.bak",
    "*.bak-*",
    "_patch_*.py",
    "*.log",
    ".deepseek_session.json",
    "deploy_targets.json",
]

# 두 저장소 공통 제외 패턴 (실수 방지용 안전망)
COMMON_EXCLUDE_PATTERNS = [
    "__pycache__/",
    "*.pyc",
    "*.tmp",
    ".DS_Store",
    "Thumbs.db",
]


# ------------------------------------------------------------------------------
# 1. 콘솔 유틸리티
# ------------------------------------------------------------------------------
def force_utf8_console() -> None:
    """콘솔 인코딩을 UTF-8 로 강제한다.

    Windows 기본 cp949 콘솔에서 UTF-8 바이트를 그대로 흘리면 한글이
    모지바케(ê¹ìì)로 깨지므로, 코드페이지를 65001 로 전환한 뒤
    스트림을 UTF-8 로 재설정한다.
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def banner(text: str) -> None:
    line = "=" * 78
    print(line)
    print(text)
    print(line)


def section(text: str) -> None:
    print("")
    print(f"[{text}]")
    print("-" * 78)


def ok(message: str) -> None:
    print(f"   [OK]   {message}")


def info(message: str) -> None:
    print(f"   [INFO] {message}")


def warn(message: str) -> None:
    print(f"   [WARN] {message}")


def fail(message: str) -> None:
    print(f"   [FAIL] {message}")


# ------------------------------------------------------------------------------
# 2. Git 실행 헬퍼
# ------------------------------------------------------------------------------
def run_git(repo: Path, args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """지정 저장소에서 git 명령을 실행하고 결과를 반환한다."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 실패 (code={completed.returncode}): "
            f"{(completed.stderr or completed.stdout or '').strip()[:500]}"
        )
    return completed


def git_out(repo: Path, args: list[str]) -> str:
    completed = run_git(repo, args)
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def run_git_stdout(repo: Path, args: list[str]) -> str:
    """git stdout 을 .strip() 없이 원문 그대로 반환한다.

    porcelain 출력은 첫 줄이 ' M path' 처럼 선행 공백을 포함하므로
    전체 strip 을 적용하면 상태 코드와 경로가 한 칸씩 밀려 파손된다.
    """
    completed = run_git(repo, args)
    if completed.returncode != 0:
        return ""
    return completed.stdout or ""


def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    completed = run_git(path, ["rev-parse", "--is-inside-work-tree"])
    return completed.returncode == 0 and (completed.stdout or "").strip() == "true"


def porcelain(repo: Path) -> list[str]:
    """git status --porcelain 원문 라인 목록 (선행 공백 보존)."""
    raw = run_git_stdout(repo, ["status", "--porcelain"])
    return [line for line in raw.splitlines() if line.strip()]


def is_excluded(path_text: str, patterns: list[str]) -> bool:
    name = Path(path_text).name
    for pattern in patterns:
        if pattern.startswith("*."):
            if name.endswith(pattern[1:]):
                return True
        elif pattern.endswith("/"):
            if pattern.rstrip("/") in path_text.replace("\\", "/").split("/"):
                return True
        elif pattern.startswith("_patch_"):
            if name.startswith("_patch_"):
                return True
        elif name == pattern:
            return True
    return False


def decode_porcelain_path(raw: str) -> str:
    """git status --porcelain 의 8진수 이스케이프 경로를 실제 유니코드로 복원.

    예) "[1\\355\\201\\264...].bat" -> "[1클릭_실행]_딥시크_안티그래피티_IDE.bat"
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    if "\\" not in text:
        return text
    try:
        decoded = text.encode("latin-1", errors="backslashreplace").decode(
            "unicode_escape"
        )
        # unicode_escape 는 UTF-8 바이트를 latin-1 문자로 흩뜨리므로 재조립
        return decoded.encode("latin-1", errors="ignore").decode("utf-8", errors="replace")
    except Exception:
        return text


# ------------------------------------------------------------------------------
# 3. 저장소 탐색
# ------------------------------------------------------------------------------
def resolve_repos() -> tuple[Path, Path | None]:
    """현재 스크립트 기준으로 두 저장소 경로를 동적으로 찾는다."""
    script_dir = Path(__file__).resolve().parent
    main_repo = script_dir if is_git_repo(script_dir) else None

    if main_repo is None:
        # 상위로 최대 3단계까지 탐색
        probe = script_dir
        for _ in range(3):
            probe = probe.parent
            if is_git_repo(probe):
                main_repo = probe
                break

    aider_repo: Path | None = None
    if main_repo is not None:
        for name in (AIDER_REPO_NAME, "aider_main", "aider"):
            candidate = main_repo.parent / name
            if is_git_repo(candidate):
                aider_repo = candidate
                break

    return (main_repo or script_dir, aider_repo)


# ------------------------------------------------------------------------------
# 4. 단일 저장소 커밋 처리
# ------------------------------------------------------------------------------
def commit_repo(
    repo: Path,
    label: str,
    commit_message: str,
    exclude_patterns: list[str],
    dry_run: bool,
) -> dict:
    result = {
        "label": label,
        "path": str(repo),
        "added": [],
        "excluded": [],
        "committed": False,
        "commit_hash": "",
        "commit_subject": "",
        "error": "",
    }

    section(f"{label} : {repo}")

    if not is_git_repo(repo):
        result["error"] = "git 저장소가 아닙니다."
        fail(result["error"])
        return result

    entries = porcelain(repo)
    if not entries:
        info("변경 사항이 없습니다. (커밋 생략)")
        return result

    # --- add 대상 선별 -------------------------------------------------------
    add_targets: list[str] = []
    for line in entries:
        status = line[:2]
        path_text = decode_porcelain_path(line[2:].lstrip(" "))
        # rename 표기 "old -> new" 대응
        if " -> " in path_text:
            path_text = decode_porcelain_path(path_text.split(" -> ", 1)[1])

        if is_excluded(path_text, COMMON_EXCLUDE_PATTERNS) or is_excluded(
            path_text, exclude_patterns
        ):
            result["excluded"].append(f"{status} {path_text}")
            continue
        add_targets.append(path_text)

    if result["excluded"]:
        info(f"제외된 항목 {len(result['excluded'])}건:")
        for item in result["excluded"]:
            info(f"  - {item}")

    if not add_targets:
        warn("add 대상이 없습니다. (커밋 생략)")
        return result

    info(f"add 대상 {len(add_targets)}건:")
    for item in add_targets:
        info(f"  + {item}")

    if dry_run:
        warn("--dry-run 모드: 실제 add/commit 을 수행하지 않았습니다.")
        result["added"] = add_targets
        return result

    # --- git add -------------------------------------------------------------
    add_proc = run_git(repo, ["add", "--", *add_targets])
    if add_proc.returncode != 0:
        result["error"] = f"git add 실패: {(add_proc.stderr or '').strip()[:400]}"
        fail(result["error"])
        return result
    result["added"] = add_targets
    ok(f"git add 완료 ({len(add_targets)}건)")

    # --- 스테이징 확인 -------------------------------------------------------
    staged = git_out(repo, ["diff", "--cached", "--name-only"])
    if not staged:
        warn("스테이징된 변경이 없습니다. (커밋 생략)")
        return result

    # --- git commit ----------------------------------------------------------
    commit_proc = run_git(repo, ["commit", "-m", commit_message])
    if commit_proc.returncode != 0:
        combined = ((commit_proc.stdout or "") + (commit_proc.stderr or "")).strip()
        result["error"] = f"git commit 실패: {combined[:400]}"
        fail(result["error"])
        return result

    result["committed"] = True
    result["commit_hash"] = git_out(repo, ["rev-parse", "--short", "HEAD"])
    result["commit_subject"] = git_out(repo, ["log", "-1", "--pretty=%s"])
    ok(f"커밋 완료: {result['commit_hash']} {result['commit_subject']}")
    return result


# ------------------------------------------------------------------------------
# 5. 푸시 (선택)
# ------------------------------------------------------------------------------
def push_repo(repo: Path, label: str) -> bool:
    section(f"{label} : 원격 푸시")
    upstream = git_out(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])

    if not upstream:
        # 최초 푸시 또는 미추적 브랜치: 현재 브랜치를 origin 에 업스트림으로 등록하며 푸시
        branch = git_out(repo, ["rev-parse", "--abbrev-ref", "HEAD"]) or "main"
        if branch == "HEAD":
            warn("detached HEAD 상태여서 푸시를 건너뜁니다.")
            return False
        info(f"업스트림 미설정 -> push -u origin {branch} 로 자동 등록 후 푸시")
        proc = run_git(repo, ["push", "-u", "origin", branch])
        if proc.returncode != 0:
            fail(f"푸시 실패: {((proc.stderr or '') + (proc.stdout or '')).strip()[:400]}")
            return False
        ok(f"푸시 완료 -> origin/{branch} (업스트림 신규 등록)")
        return True

    proc = run_git(repo, ["push"])
    if proc.returncode != 0:
        fail(f"푸시 실패: {((proc.stderr or '') + (proc.stdout or '')).strip()[:400]}")
        return False
    ok(f"푸시 완료 -> {upstream}")
    return True


# ------------------------------------------------------------------------------
# 6. 메인
# ------------------------------------------------------------------------------
def main() -> int:
    force_utf8_console()

    parser = argparse.ArgumentParser(
        description="Git Deployment Guardian - '깃에저장' 공식 명령 집행기"
    )
    parser.add_argument("--push", action="store_true", help="커밋 후 원격 푸시 수행")
    parser.add_argument("--dry-run", action="store_true", help="실제 반영 없이 계획만 출력")
    args = parser.parse_args()

    banner(" Git Deployment Guardian - '깃에저장' 집행 ")

    main_repo, aider_repo = resolve_repos()
    info(f"메인 저장소 : {main_repo}")
    info(f"aider 저장소: {aider_repo if aider_repo else '(미탐색)'}")
    if args.dry_run:
        warn("DRY-RUN 모드로 실행 중입니다.")

    results: list[dict] = []

    results.append(
        commit_repo(
            repo=main_repo,
            label=MAIN_REPO_NAME,
            commit_message=MAIN_COMMIT_MSG,
            exclude_patterns=[],
            dry_run=args.dry_run,
        )
    )

    if aider_repo is not None:
        results.append(
            commit_repo(
                repo=aider_repo,
                label=AIDER_REPO_NAME,
                commit_message=AIDER_COMMIT_MSG,
                exclude_patterns=AIDER_EXCLUDE_PATTERNS,
                dry_run=args.dry_run,
            )
        )
    else:
        warn("aider-main 저장소를 찾지 못해 건너뜁니다.")

    # --- 푸시 ---------------------------------------------------------------
    pushed: list[str] = []
    if args.push and not args.dry_run:
        for repo, label in ((main_repo, MAIN_REPO_NAME), (aider_repo, AIDER_REPO_NAME)):
            if repo is None:
                continue
            if push_repo(repo, label):
                pushed.append(label)

    # --- 최종 보고 -----------------------------------------------------------
    banner(" 집행 결과 요약 ")
    exit_code = 0
    for item in results:
        print(f"  ▶ {item['label']}")
        print(f"     경로      : {item['path']}")
        print(f"     add 건수  : {len(item['added'])}")
        print(f"     제외 건수 : {len(item['excluded'])}")
        if item["committed"]:
            print(f"     커밋      : {item['commit_hash']} {item['commit_subject']}")
        elif item["error"]:
            print(f"     오류      : {item['error']}")
            exit_code = 1
        else:
            print("     커밋      : (변경 없음 / 생략)")
        print("")

    if args.push:
        print(f"  원격 푸시 : {', '.join(pushed) if pushed else '수행 없음'}")
    else:
        print("  원격 푸시 : 대기 (대표님 추가 지시 시 --push 로 집행)")

    # --- 실측 상태 재확인 ----------------------------------------------------
    section("실측 git status (커밋 후 잔여 변경)")
    for repo, label in ((main_repo, MAIN_REPO_NAME), (aider_repo, AIDER_REPO_NAME)):
        if repo is None:
            continue
        remaining = porcelain(repo)
        print(f"  {label}: 잔여 {len(remaining)}건")
        for line in remaining[:20]:
            print(f"    {line}")
        if len(remaining) > 20:
            print(f"    ... 외 {len(remaining) - 20}건")

    section("실측 최근 커밋 로그")
    for repo, label in ((main_repo, MAIN_REPO_NAME), (aider_repo, AIDER_REPO_NAME)):
        if repo is None:
            continue
        print(f"  {label}:")
        log = git_out(repo, ["log", "--oneline", "-3"])
        for line in log.splitlines():
            print(f"    {line}")

    print("")
    if exit_code == 0:
        print("  '깃에저장' 집행이 정상 완료되었습니다.")
    else:
        print("  일부 저장소에서 오류가 발생했습니다. 위 로그를 확인하십시오.")
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n사용자에 의해 중단되었습니다.")
        sys.exit(130)
