#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
 DeepSeek Antigravity IDE - 통합 자율 런처 (Standalone Launcher)
================================================================================
 사용법:
     python launch_ide.py

 목적:
     핵심 폴더 2개(DeepSeek_Antigravity_IDE, aider-main)를 USB / Git 으로
     다른 컴퓨터에 복사한 뒤, 단 1줄의 파이썬 명령으로 즉시 실행한다.

     - 하드코딩된 사용자명/경로를 일절 사용하지 않는다.
     - 새 컴퓨터의 사용자명, 바탕화면 위치, 파이썬 경로에 100% 자동 적응한다.
     - 외부 비표준 라이브러리 없이 파이썬 표준 라이브러리만 사용한다.

 동작 흐름:
     [Step 1] 동적 경로 탐색
     [Step 2] 딥시크 5인 협업체 MCP 서버 전역 등록
     [Step 3] 게이트키퍼 훅 동기화
     [Step 4] 통합 워크스페이스 갱신
     [Step 5] 바탕화면 바로가기 자동 생성
     [Step 6] Antigravity IDE 실행
================================================================================
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# ------------------------------------------------------------------------------
# 0. 전역 상수 / 콘솔 UTF-8 강제
# ------------------------------------------------------------------------------
APP_TITLE = "DeepSeek Antigravity IDE"
SHORTCUT_NAME = "딥시크 안티그래피티 IDE.lnk"
WORKSPACE_FILENAME = "DeepSeek_Antigravity.code-workspace"
MCP_SERVER_NAME = "deepseek-council"
GATEKEEPER_HOOK_NAME = "gatekeeper_hook.py"

# IDE 실행 파일 후보 (표준 설치 경로 - 순차 탐색)
IDE_EXE_RELATIVE_CANDIDATES = [
    ("LOCALAPPDATA", r"Programs\DeepSeek Antigravity IDE\Antigravity IDE.exe"),
    ("LOCALAPPDATA", r"Programs\Antigravity IDE\Antigravity IDE.exe"),
    ("LOCALAPPDATA", r"Programs\DeepSeek Antigravity IDE\Antigravity.exe"),
    ("LOCALAPPDATA", r"Programs\Antigravity IDE\Antigravity.exe"),
    ("PROGRAMFILES", r"DeepSeek Antigravity IDE\Antigravity IDE.exe"),
    ("PROGRAMFILES", r"Antigravity IDE\Antigravity IDE.exe"),
    ("PROGRAMFILES(X86)", r"DeepSeek Antigravity IDE\Antigravity IDE.exe"),
    ("PROGRAMFILES(X86)", r"Antigravity IDE\Antigravity IDE.exe"),
    ("LOCALAPPDATA", r"Programs\Microsoft VS Code\Code.exe"),
]

# 바탕화면 후보 (OneDrive 리디렉션 / 다중 사용자 환경 대응)
DESKTOP_SUBDIRS = [
    ("USERPROFILE", "Desktop"),
    ("USERPROFILE", r"OneDrive\Desktop"),
    ("USERPROFILE", r"OneDrive - Personal\Desktop"),
    ("USERPROFILE", r"바탕 화면"),
    ("PUBLIC", "Desktop"),
]

STEP_TOTAL = 6


# ------------------------------------------------------------------------------
# 1. 콘솔 / 로그 유틸리티
# ------------------------------------------------------------------------------
def force_utf8_console() -> None:
    """Windows 콘솔(cp949) 환경에서도 한글/이모지가 깨지지 않도록 UTF-8 강제."""
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


def step(index: int, title: str) -> None:
    print("")
    print(f"[Step {index}/{STEP_TOTAL}] {title}")
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
# 2. 파일 시스템 유틸리티 (원자적 쓰기 / 안전한 JSON)
# ------------------------------------------------------------------------------
def atomic_write_text(target: Path, content: str, encoding: str = "utf-8") -> None:
    """임시 파일에 먼저 쓴 뒤 os.replace 로 원자적 교체 (부분 기록 손상 방지)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def atomic_copy_file(source: Path, target: Path) -> None:
    """원자적 파일 복사 (게이트키퍼 훅 동기화용)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    os.close(fd)
    try:
        shutil.copyfile(str(source), tmp_path)
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def load_json_safe(path: Path) -> dict:
    """JSON 을 읽되, 없거나 깨졌으면 빈 dict 반환 (BOM/인코딩 방어)."""
    if not path.exists():
        return {}
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            raw = path.read_text(encoding=encoding)
        except Exception:
            continue
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
        return {}
    return {}


def dump_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def env_path(key: str) -> Path | None:
    value = os.environ.get(key)
    if not value:
        return None
    return Path(value)


# ------------------------------------------------------------------------------
# 3. [Step 1] 동적 경로 탐색
# ------------------------------------------------------------------------------
class Paths:
    """새 컴퓨터 환경에 자동 적응한 실제 경로 묶음."""

    def __init__(self) -> None:
        self.script_file: Path = Path(__file__).resolve()
        self.project_root: Path = self.script_file.parent
        self.project_name: str = self.project_root.name
        self.home: Path = Path.home()
        self.gemini_config: Path = self.home / ".gemini" / "config"
        self.desktop: Path | None = None
        self.aider_root: Path | None = None
        self.mcp_server: Path = self.project_root / "backend" / "core" / "mcp_server.py"
        self.gatekeeper_source: Path = (
            self.project_root / ".agents" / "scripts" / GATEKEEPER_HOOK_NAME
        )
        self.workspace_file: Path = self.project_root / WORKSPACE_FILENAME
        self.python_exe: str = sys.executable or "python"

    # -- 바탕화면 탐색 ---------------------------------------------------------
    def resolve_desktop(self) -> Path | None:
        candidates: list[Path] = []
        for env_key, sub in DESKTOP_SUBDIRS:
            base = env_path(env_key)
            if base is None:
                continue
            candidates.append(base / sub)
        # 최후 수단: 홈 디렉터리 하위 Desktop
        candidates.append(self.home / "Desktop")

        for candidate in candidates:
            try:
                if candidate.is_dir():
                    self.desktop = candidate
                    return candidate
            except OSError:
                continue
        return None

    # -- aider-main 탐색 -------------------------------------------------------
    def resolve_aider(self) -> Path | None:
        """프로젝트와 '인접한' 위치에서 aider-main 을 동적으로 찾는다."""
        search_roots: list[Path] = [self.project_root.parent, self.project_root]
        # 상위 2단계까지 확장 (USB 루트에 두 폴더를 나란히 복사한 경우 대응)
        parent = self.project_root.parent
        if parent.parent != parent:
            search_roots.append(parent.parent)

        names = ["aider-main", "aider_main", "aider"]
        seen: set[str] = set()
        for root in search_roots:
            key = str(root).lower()
            if key in seen:
                continue
            seen.add(key)
            for name in names:
                candidate = root / name
                try:
                    if candidate.is_dir():
                        self.aider_root = candidate
                        return candidate
                except OSError:
                    continue
        return None


def step1_resolve_paths(paths: Paths) -> None:
    step(1, "동적 경로 탐색 (새 컴퓨터 환경 자동 적응)")
    info(f"스크립트 위치   : {paths.script_file}")
    info(f"프로젝트 루트   : {paths.project_root}")
    info(f"사용자 홈       : {paths.home}")
    info(f"Gemini 설정     : {paths.gemini_config}")
    info(f"파이썬 실행기   : {paths.python_exe}")

    desktop = paths.resolve_desktop()
    if desktop:
        ok(f"바탕화면 확인   : {desktop}")
    else:
        warn("바탕화면 경로를 찾지 못했습니다. (Step 5 바로가기 생성은 건너뜁니다)")

    aider = paths.resolve_aider()
    if aider:
        ok(f"aider-main 확인 : {aider}")
    else:
        warn("인접 위치에서 aider-main 을 찾지 못했습니다. (워크스페이스에서 자동 제외)")

    if paths.mcp_server.exists():
        ok(f"MCP 서버 확인   : {paths.mcp_server}")
    else:
        warn(f"MCP 서버 파일 없음: {paths.mcp_server}")

    if paths.gatekeeper_source.exists():
        ok(f"게이트키퍼 원본 : {paths.gatekeeper_source}")
    else:
        warn(f"게이트키퍼 원본 없음: {paths.gatekeeper_source}")


# ------------------------------------------------------------------------------
# 4. [Step 2] MCP 서버 전역 등록
# ------------------------------------------------------------------------------
def step2_register_mcp(paths: Paths) -> None:
    step(2, f"딥시크 5인 협업체 MCP 서버 전역 등록 ({MCP_SERVER_NAME})")
    config_path = paths.gemini_config / "mcp_config.json"
    config = load_json_safe(config_path)

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers

    previous = servers.get(MCP_SERVER_NAME)
    servers[MCP_SERVER_NAME] = {
        "command": paths.python_exe,
        "args": [str(paths.mcp_server)],
        "env": {"PYTHONIOENCODING": "utf-8"},
    }

    try:
        atomic_write_text(config_path, dump_json(config), encoding="utf-8")
    except Exception as exc:
        fail(f"mcp_config.json 기록 실패: {exc}")
        return

    if previous is None:
        ok(f"신규 등록 완료: {config_path}")
    else:
        ok(f"기존 항목 갱신 완료: {config_path}")
    info(f"실행 인자: {paths.python_exe} \"{paths.mcp_server}\"")


# ------------------------------------------------------------------------------
# 5. [Step 3] 게이트키퍼 훅 동기화
# ------------------------------------------------------------------------------
def build_hooks_payload(hook_target: Path) -> dict:
    command = f'python "{hook_target}"'
    return {
        "hooks": {
            "PreInvocation": [
                {
                    "type": "command",
                    "command": f"{command} --event PreInvocation",
                    "timeout": 15000,
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command} --event PreToolUse",
                            "timeout": 15000,
                        }
                    ],
                }
            ],
        }
    }


def step3_sync_gatekeeper(paths: Paths) -> None:
    step(3, "게이트키퍼 훅 동기화")
    hook_target = paths.gemini_config / GATEKEEPER_HOOK_NAME

    if paths.gatekeeper_source.exists():
        try:
            atomic_copy_file(paths.gatekeeper_source, hook_target)
            ok(f"훅 스크립트 원자적 복사 완료: {hook_target}")
        except Exception as exc:
            fail(f"훅 스크립트 복사 실패: {exc}")
    else:
        warn("원본 훅 스크립트가 없어 복사를 건너뜁니다.")

    hooks_path = paths.gemini_config / "hooks.json"
    payload = build_hooks_payload(hook_target)
    try:
        atomic_write_text(hooks_path, dump_json(payload), encoding="utf-8")
        ok(f"hooks.json 자동 생성/갱신 완료: {hooks_path}")
    except Exception as exc:
        fail(f"hooks.json 기록 실패: {exc}")


# ------------------------------------------------------------------------------
# 6. [Step 4] 통합 워크스페이스 갱신
# ------------------------------------------------------------------------------
def step4_update_workspace(paths: Paths) -> None:
    step(4, "통합 워크스페이스 갱신")
    folders: list[dict] = []

    if paths.aider_root is not None:
        folders.append(
            {"name": paths.aider_root.name, "path": paths.aider_root.as_posix()}
        )
    folders.append(
        {"name": paths.project_name, "path": paths.project_root.as_posix()}
    )

    workspace = {"folders": folders, "settings": {}}
    try:
        atomic_write_text(paths.workspace_file, dump_json(workspace), encoding="utf-8")
        ok(f"워크스페이스 갱신 완료: {paths.workspace_file}")
        for folder in folders:
            info(f"  - {folder['name']}: {folder['path']}")
    except Exception as exc:
        fail(f"워크스페이스 기록 실패: {exc}")


# ------------------------------------------------------------------------------
# 7. [Step 5] 바탕화면 바로가기 생성
# ------------------------------------------------------------------------------
def ps_quote(value: str) -> str:
    """PowerShell 작은따옴표 문자열 이스케이프."""
    return "'" + value.replace("'", "''") + "'"


def step5_create_shortcut(paths: Paths) -> None:
    step(5, "바탕화면 바로가기 자동 생성")
    if paths.desktop is None:
        warn("바탕화면을 찾지 못해 바로가기 생성을 건너뜁니다.")
        return

    shortcut_path = paths.desktop / SHORTCUT_NAME
    target_exe = paths.python_exe
    arguments = f'"{paths.script_file}"'
    working_dir = str(paths.project_root)
    icon_location = f"{target_exe},0"

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut({ps_quote(str(shortcut_path))}); "
        f"$sc.TargetPath = {ps_quote(target_exe)}; "
        f"$sc.Arguments = {ps_quote(arguments)}; "
        f"$sc.WorkingDirectory = {ps_quote(working_dir)}; "
        f"$sc.IconLocation = {ps_quote(icon_location)}; "
        f"$sc.Description = {ps_quote(APP_TITLE + ' 자동 런처')}; "
        "$sc.WindowStyle = 1; "
        "$sc.Save(); "
        "Write-Output 'SHORTCUT_CREATED'"
    )

    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except FileNotFoundError:
        warn("PowerShell 을 찾지 못해 바로가기 생성을 건너뜁니다.")
        return
    except subprocess.TimeoutExpired:
        fail("PowerShell 바로가기 생성이 시간 초과되었습니다.")
        return
    except Exception as exc:
        fail(f"바로가기 생성 중 예외: {exc}")
        return

    if completed.returncode == 0 and shortcut_path.exists():
        ok(f"바로가기 생성 완료: {shortcut_path}")
    else:
        stderr = (completed.stderr or "").strip()
        fail(f"바로가기 생성 실패 (code={completed.returncode}) {stderr[:300]}")


# ------------------------------------------------------------------------------
# 8. [Step 6] Antigravity IDE 실행
# ------------------------------------------------------------------------------
def find_ide_executable() -> Path | None:
    for env_key, relative in IDE_EXE_RELATIVE_CANDIDATES:
        base = env_path(env_key)
        if base is None:
            continue
        candidate = base / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def step6_launch_ide(paths: Paths) -> bool:
    step(6, "Antigravity IDE 실행")
    ide_exe = find_ide_executable()

    if ide_exe is None:
        warn("표준 설치 경로에서 Antigravity IDE 실행 파일을 찾지 못했습니다.")
        info("탐색한 경로:")
        for env_key, relative in IDE_EXE_RELATIVE_CANDIDATES:
            base = env_path(env_key)
            shown = str(base / relative) if base else f"%{env_key}%\\{relative}"
            info(f"  - {shown}")
        info("IDE 미설치 상태입니다. 아래 워크스페이스 파일을 IDE 에서 직접 열어주세요.")
        info(f"  워크스페이스: {paths.workspace_file}")
        return False

    ok(f"IDE 실행 파일 발견: {ide_exe}")
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        subprocess.Popen(
            [str(ide_exe), str(paths.workspace_file)],
            cwd=str(paths.project_root),
            creationflags=creationflags,
            close_fds=True,
        )
        ok("워크스페이스를 인자로 전달하여 백그라운드 기동했습니다.")
        return True
    except Exception as exc:
        fail(f"IDE 기동 실패: {exc}")
        info(f"수동 실행: \"{ide_exe}\" \"{paths.workspace_file}\"")
        return False


# ------------------------------------------------------------------------------
# 9. 메인
# ------------------------------------------------------------------------------
def main() -> int:
    force_utf8_console()
    banner(f" {APP_TITLE} - 통합 자율 런처 (Standalone) ")

    paths = Paths()
    failures: list[str] = []

    try:
        step1_resolve_paths(paths)
    except Exception as exc:
        failures.append(f"Step1 경로 탐색 실패: {exc}")
        traceback.print_exc()

    for index, (label, func) in enumerate(
        [
            ("Step2 MCP 등록", lambda: step2_register_mcp(paths)),
            ("Step3 게이트키퍼 동기화", lambda: step3_sync_gatekeeper(paths)),
            ("Step4 워크스페이스 갱신", lambda: step4_update_workspace(paths)),
            ("Step5 바로가기 생성", lambda: step5_create_shortcut(paths)),
        ],
        start=2,
    ):
        try:
            func()
        except Exception as exc:
            failures.append(f"{label} 실패: {exc}")
            traceback.print_exc()

    launched = False
    try:
        launched = step6_launch_ide(paths)
    except Exception as exc:
        failures.append(f"Step6 IDE 실행 실패: {exc}")
        traceback.print_exc()

    print("")
    banner(" 실행 결과 요약 ")
    print(f"  프로젝트 루트 : {paths.project_root}")
    print(f"  워크스페이스  : {paths.workspace_file}")
    print(f"  MCP 설정      : {paths.gemini_config / 'mcp_config.json'}")
    print(f"  훅 설정       : {paths.gemini_config / 'hooks.json'}")
    print(f"  바탕화면      : {paths.desktop if paths.desktop else '(미탐색)'}")
    print(f"  IDE 기동      : {'성공' if launched else '미기동 (수동 실행 필요)'}")

    if failures:
        print("")
        print("  [경고] 일부 단계가 실패했습니다:")
        for item in failures:
            print(f"    - {item}")
        return 1

    print("")
    print("  모든 단계가 정상 완료되었습니다. 대표님, 바로 사용하십시오.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n사용자에 의해 중단되었습니다.")
        sys.exit(130)
