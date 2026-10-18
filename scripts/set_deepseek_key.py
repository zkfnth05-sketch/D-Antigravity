#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
set_deepseek_key.py — DEEPSEEK_API_KEY 원클릭 영구 등록 도구 (Windows)

새 컴퓨터에서 파이썬 명령 한 줄로 DEEPSEEK_API_KEY 를
윈도우 '영구 사용자 환경변수'(HKCU\\Environment)에 등록하고,
실행 중인 프로세스(탐색기/터미널)에 변경 사실을 브로드캐스트합니다.

사용법
------
  1) python scripts/set_deepseek_key.py sk-xxxxxxxxxxxx
  2) python scripts/set_deepseek_key.py --key sk-xxxxxxxxxxxx
  3) python scripts/set_deepseek_key.py            (대화형 입력, 화면에 키 안 보임)

추가 옵션
---------
  --show        현재 등록된 키를 마스킹해서 보여주고 종료
  --remove      등록된 DEEPSEEK_API_KEY 를 삭제
  --no-broadcast  브로드캐스트 생략 (레지스트리 기록만)
  --yes, -y     확인 프롬프트 없이 즉시 실행

동작 원리
---------
  * winreg 로 HKCU\\Environment 에 REG_SZ 값 기록 (영구, 재부팅 후에도 유지)
  * SendMessageTimeoutW(WM_SETTINGCHANGE, "Environment") 로 브로드캐스트
    → 새로 여는 cmd/PowerShell/탐색기에서 즉시 인식
  * 키는 항상 마스킹(sk-****1234)해서 출력 → 화면/로그 유출 방지

보안 주의
---------
  * 이 스크립트는 키를 디스크에 저장하지 않습니다(레지스트리만 사용).
  * .gitignore 에 .env / .env.* 가 등록되어 있어야 하며,
    키를 코드/노트북에 하드코딩하지 마십시오.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from ctypes import wintypes

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
ENV_NAME = "DEEPSEEK_API_KEY"
REG_PATH = r"Environment"          # HKCU\Environment
KEY_PREFIX = "sk-"
MIN_KEY_LEN = 16                   # 지나치게 짧은 입력 방어

# SendMessageTimeoutW 관련 상수
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002
BROADCAST_TIMEOUT_MS = 5000

# 콘솔 색상 (Windows 10+ 에서 동작, 실패해도 무해)
_COLORS = {
    "ok": "\033[92m",
    "warn": "\033[93m",
    "err": "\033[91m",
    "info": "\033[96m",
    "dim": "\033[90m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


# ---------------------------------------------------------------------------
# 출력 헬퍼
# ---------------------------------------------------------------------------
def _enable_ansi() -> None:
    """Windows 콘솔에서 ANSI 이스케이프 시퀀스를 활성화한다."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _c(text: str, color: str) -> str:
    return f"{_COLORS.get(color, '')}{text}{_COLORS['reset']}"


def info(msg: str) -> None:
    print(f"{_c('[INFO]', 'info')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('[ OK ]', 'ok')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('[WARN]', 'warn')} {msg}")


def err(msg: str) -> None:
    print(f"{_c('[FAIL]', 'err')} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 키 마스킹
# ---------------------------------------------------------------------------
def mask_key(key: str) -> str:
    """
    API 키를 안전하게 마스킹한다.

    sk-abcdefghijklmnop1234  ->  sk-****1234
    짧은 키는 앞 3자 + **** 로 처리해 전체 노출을 막는다.
    """
    if not key:
        return "(없음)"

    key = key.strip()

    # 접두사(sk-) 분리
    if key.startswith(KEY_PREFIX):
        prefix, body = KEY_PREFIX, key[len(KEY_PREFIX):]
    else:
        prefix, body = "", key

    if len(body) <= 4:
        # 본문이 4자 이하면 뒤 4자를 그대로 쓰면 전부 노출되므로 가린다
        return f"{prefix}****"

    return f"{prefix}****{body[-4:]}"


# ---------------------------------------------------------------------------
# 레지스트리 입출력
# ---------------------------------------------------------------------------
def _require_windows() -> None:
    if os.name != "nt":
        err("이 스크립트는 Windows 전용입니다 (winreg / HKCU\\Environment 사용).")
        err(f"현재 OS: {os.name} — macOS/Linux 에서는 셸 프로필에 export 하십시오.")
        sys.exit(2)


def read_env_from_registry(name: str = ENV_NAME) -> str | None:
    """HKCU\\Environment 에서 값을 읽는다. 없으면 None."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ
        ) as hkey:
            value, _ = winreg.QueryValueEx(hkey, name)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def write_env_to_registry(value: str, name: str = ENV_NAME) -> None:
    """HKCU\\Environment 에 REG_SZ 로 영구 기록한다."""
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as hkey:
        winreg.SetValueEx(hkey, name, 0, winreg.REG_SZ, value)


def delete_env_from_registry(name: str = ENV_NAME) -> bool:
    """HKCU\\Environment 에서 값을 삭제한다. 삭제했으면 True."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as hkey:
            winreg.DeleteValue(hkey, name)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 환경변수 브로드캐스트
# ---------------------------------------------------------------------------
def broadcast_environment_change(timeout_ms: int = BROADCAST_TIMEOUT_MS) -> bool:
    """
    WM_SETTINGCHANGE("Environment") 를 모든 최상위 창에 브로드캐스트한다.

    성공하면 True. 실패해도 레지스트리 값은 이미 기록되어 있으므로
    '새 터미널을 열면 반영됨' 상태는 유지된다.
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        send_message_timeout = user32.SendMessageTimeoutW
        send_message_timeout.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        send_message_timeout.restype = wintypes.LPARAM

        result = ctypes.c_size_t(0)
        ret = send_message_timeout(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ctypes.cast(ctypes.c_wchar_p("Environment"), wintypes.LPARAM),
            SMTO_ABORTIFHUNG,
            timeout_ms,
            ctypes.byref(result),
        )
        return bool(ret)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 키 검증
# ---------------------------------------------------------------------------
def validate_key(key: str) -> tuple[bool, str]:
    """키 형식 검증. (통과여부, 사유)"""
    if not key:
        return False, "키가 비어 있습니다."
    if key != key.strip():
        return False, "키 앞뒤에 공백/개행이 포함되어 있습니다."
    if any(ch.isspace() for ch in key):
        return False, "키 내부에 공백이 포함되어 있습니다."
    if len(key) < MIN_KEY_LEN:
        return False, f"키가 너무 짧습니다 ({len(key)}자, 최소 {MIN_KEY_LEN}자)."
    if not key.startswith(KEY_PREFIX):
        # DeepSeek 키는 보통 sk- 로 시작하지만, 강제 실패시키지는 않는다.
        return True, f"경고: '{KEY_PREFIX}' 접두사로 시작하지 않습니다."
    return True, ""


# ---------------------------------------------------------------------------
# 대화형 입력
# ---------------------------------------------------------------------------
def prompt_key_interactive() -> str:
    """인자 없이 실행됐을 때 키를 안전하게 입력받는다 (화면에 표시 안 함)."""
    import getpass

    print()
    info(f"{ENV_NAME} 값을 입력하세요. (입력 내용은 화면에 표시되지 않습니다)")
    try:
        key = getpass.getpass("  API Key > ")
    except (KeyboardInterrupt, EOFError):
        print()
        err("입력이 취소되었습니다.")
        sys.exit(1)
    return key.strip()


# ---------------------------------------------------------------------------
# 확인 프롬프트
# ---------------------------------------------------------------------------
def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return answer in ("y", "yes", "ㅛ")


# ---------------------------------------------------------------------------
# 서브 커맨드
# ---------------------------------------------------------------------------
def cmd_show() -> int:
    _require_windows()
    current = read_env_from_registry()
    print()
    if current is None:
        warn(f"{ENV_NAME} 가 HKCU\\Environment 에 등록되어 있지 않습니다.")
        return 1

    ok(f"{ENV_NAME} = {_c(mask_key(current), 'bold')}")
    detail = f"(총 {len(current)}자, 레지스트리 HKCU\\Environment)"
    print(f"       {_c(detail, 'dim')}")

    live = os.environ.get(ENV_NAME)
    if live is None:
        warn("현재 이 프로세스에는 아직 반영되지 않았습니다. 새 터미널을 여십시오.")
    elif live == current:
        ok("현재 프로세스에도 정상 반영되어 있습니다.")
    else:
        warn("현재 프로세스의 값이 레지스트리와 다릅니다. 새 터미널을 여십시오.")
    return 0


def cmd_remove(assume_yes: bool) -> int:
    _require_windows()
    current = read_env_from_registry()
    if current is None:
        warn(f"{ENV_NAME} 는 이미 등록되어 있지 않습니다.")
        return 1

    print()
    info(f"삭제 대상: {ENV_NAME} = {_c(mask_key(current), 'bold')}")
    if not confirm("정말 삭제하시겠습니까?", assume_yes):
        warn("사용자가 취소했습니다.")
        return 1

    if delete_env_from_registry():
        ok(f"{ENV_NAME} 를 레지스트리에서 삭제했습니다.")
    else:
        err("레지스트리 삭제에 실패했습니다.")
        return 1

    if broadcast_environment_change():
        ok("환경변수 변경을 브로드캐스트했습니다.")
    else:
        warn("브로드캐스트 실패 — 새 터미널을 열면 반영됩니다.")

    # 현재 프로세스에서도 제거
    os.environ.pop(ENV_NAME, None)
    return 0


def cmd_set(key: str, assume_yes: bool, do_broadcast: bool) -> int:
    _require_windows()

    valid, reason = validate_key(key)
    if not valid:
        err(f"키 검증 실패: {reason}")
        return 1
    if reason:
        warn(reason)

    masked = mask_key(key)
    previous = read_env_from_registry()

    print()
    print(_c("─" * 58, "dim"))
    print(f"  대상 변수 : {_c(ENV_NAME, 'bold')}")
    print("  저장 위치 : HKCU\\Environment  (영구 사용자 환경변수)")
    print(f"  새 키     : {_c(masked, 'bold')}  ({len(key)}자)")
    if previous is not None:
        print(f"  기존 키   : {_c(mask_key(previous), 'dim')}  (덮어씁니다)")
    else:
        print(f"  기존 키   : {_c('(없음 — 신규 등록)', 'dim')}")
    print(_c("─" * 58, "dim"))

    if not confirm("이대로 등록하시겠습니까?", assume_yes):
        warn("사용자가 취소했습니다.")
        return 1

    # 1) 레지스트리 영구 기록
    try:
        write_env_to_registry(key)
    except PermissionError:
        err("레지스트리 쓰기 권한이 없습니다. 일반 사용자 권한으로 다시 시도하십시오.")
        return 1
    except OSError as exc:
        err(f"레지스트리 기록 실패: {exc}")
        return 1
    ok(f"HKCU\\Environment 에 {ENV_NAME} 를 기록했습니다.")

    # 2) 기록 검증 (읽어와서 대조)
    stored = read_env_from_registry()
    if stored != key:
        err("기록 후 검증에 실패했습니다. 레지스트리 값을 다시 확인하십시오.")
        return 1
    ok("레지스트리 기록 검증 통과.")

    # 3) 현재 프로세스에도 즉시 반영
    os.environ[ENV_NAME] = key
    ok("현재 프로세스 환경변수에 즉시 반영했습니다.")

    # 4) 브로드캐스트
    if do_broadcast:
        if broadcast_environment_change():
            ok("WM_SETTINGCHANGE 브로드캐스트 완료 — 새 터미널에서 즉시 인식됩니다.")
        else:
            warn("브로드캐스트 실패 — 새 터미널을 열면 반영됩니다.")
    else:
        info("브로드캐스트를 생략했습니다 (--no-broadcast).")

    print()
    ok(f"완료: {ENV_NAME} = {_c(masked, 'bold')}")
    print(_c("  · 이미 열려 있는 터미널은 재시작해야 반영됩니다.", "dim"))
    print(_c("  · 확인: python scripts/set_deepseek_key.py --show", "dim"))
    return 0


# ---------------------------------------------------------------------------
# 인자 파싱
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_deepseek_key.py",
        description="DEEPSEEK_API_KEY 를 윈도우 영구 사용자 환경변수(HKCU\\Environment)에 등록합니다.",
        epilog=(
            "예시:\n"
            "  python scripts/set_deepseek_key.py sk-xxxxxxxxxxxx\n"
            "  python scripts/set_deepseek_key.py --key sk-xxxxxxxxxxxx\n"
            "  python scripts/set_deepseek_key.py            (대화형 입력)\n"
            "  python scripts/set_deepseek_key.py --show     (현재 값 확인)\n"
            "  python scripts/set_deepseek_key.py --remove   (삭제)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "positional_key",
        nargs="?",
        metavar="API_KEY",
        help="DEEPSEEK_API_KEY 값 (위치 인자)",
    )
    parser.add_argument(
        "--key",
        "-k",
        dest="option_key",
        metavar="API_KEY",
        help="DEEPSEEK_API_KEY 값 (옵션 인자)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="현재 등록된 키를 마스킹해서 출력하고 종료",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="등록된 DEEPSEEK_API_KEY 를 삭제",
    )
    parser.add_argument(
        "--no-broadcast",
        action="store_true",
        help="환경변수 브로드캐스트를 생략",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="확인 프롬프트 없이 즉시 실행",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _enable_ansi()
    parser = build_parser()
    args = parser.parse_args(argv)

    # --show / --remove 는 상호 배타적
    if args.show and args.remove:
        err("--show 와 --remove 는 함께 사용할 수 없습니다.")
        return 2

    if args.show:
        return cmd_show()

    if args.remove:
        return cmd_remove(args.yes)

    # 키 결정: 위치 인자 > --key > 대화형
    if args.positional_key and args.option_key:
        err("위치 인자와 --key 를 동시에 지정할 수 없습니다. 하나만 사용하십시오.")
        return 2

    key = args.positional_key or args.option_key
    if key is None:
        key = prompt_key_interactive()

    if not key:
        err("키가 비어 있습니다. 다시 실행하십시오.")
        return 1

    return cmd_set(key, args.yes, do_broadcast=not args.no_broadcast)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        err("사용자에 의해 중단되었습니다.")
        sys.exit(130)
