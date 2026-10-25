#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search_code.py — 코드 구조 검색 도구
================================================================================
[목적] 딥시크 5인 협업체가 코드베이스 전체를 구조적으로 검색할 수 있게 한다.
       단순 텍스트 검색(grep)을 넘어 심볼 단위 검색, 참조 추적, 구조 탐색 지원.

기능:
  1. search_symbols(directory, pattern)     — 함수/클래스/메서드 이름 검색
  2. search_references(directory, symbol)   — 심볼 참조(호출/사용) 위치 검색
  3. search_content(directory, query, ctx)  — 내용 검색 + 전후 컨텍스트
  4. search_structure(directory, kind)       — 종류별 구조 검색 (function/class 등)
  5. search_files(directory, pattern)        — 파일명 패턴 검색
  6. search_todo(directory)                  — TODO/FIXME/HACK 코멘트 검색

사용법 (CLI):
  python search_code.py symbols <directory> <pattern>
  python search_code.py references <directory> <symbol>
  python search_code.py content <directory> <query> [--context 3]
  python search_code.py structure <directory> --kind class
  python search_code.py files <directory> <pattern>
  python search_code.py todo <directory>

사용법 (모듈):
  from search_code import CodeSearcher
  searcher = CodeSearcher()
  results = searcher.search_symbols("/path/to/project", "my_func")

작성: Gemini (대표님 승인 하 단독 구현)
================================================================================
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# code_eye 모듈 임포트 (동일 디렉터리)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
try:
    from code_eye import CodeEye, Symbol, FileOutline
except ImportError:
    CodeEye = None  # type: ignore


# ============================================================
# 0. 데이터 구조
# ============================================================

@dataclass
class SearchMatch:
    """검색 결과 단일 매칭."""
    filepath: str           # 절대 경로
    rel_path: str           # 상대 경로
    line_number: int        # 줄 번호 (1-indexed)
    line_content: str       # 해당 줄 내용
    context_before: List[str] = field(default_factory=list)  # 전 컨텍스트
    context_after: List[str] = field(default_factory=list)   # 후 컨텍스트
    symbol_name: str = ""   # 관련 심볼 이름
    symbol_kind: str = ""   # 심볼 종류
    match_type: str = ""    # 매칭 종류 (definition, reference, content, todo)


@dataclass
class SearchResult:
    """검색 결과 전체."""
    query: str
    total_matches: int = 0
    files_scanned: int = 0
    files_matched: int = 0
    matches: List[SearchMatch] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_text(self) -> str:
        """사람이 읽기 좋은 텍스트 형식."""
        lines = []
        lines.append(f"🔍 검색: '{self.query}'")
        lines.append(f"   {self.files_scanned}개 파일 스캔 → {self.total_matches}건 매칭 ({self.files_matched}개 파일)")
        lines.append("")

        # 파일별 그룹핑
        by_file: Dict[str, List[SearchMatch]] = {}
        for m in self.matches:
            if m.rel_path not in by_file:
                by_file[m.rel_path] = []
            by_file[m.rel_path].append(m)

        for rel_path, file_matches in sorted(by_file.items()):
            lines.append(f"📄 {rel_path} ({len(file_matches)}건)")
            for m in file_matches:
                prefix = f"  L{m.line_number:>5}"
                if m.symbol_name:
                    prefix += f" [{m.symbol_kind}:{m.symbol_name}]"
                lines.append(f"{prefix}: {m.line_content.rstrip()}")
                for ctx in m.context_before:
                    lines.append(f"         {ctx.rstrip()}")
                for ctx in m.context_after:
                    lines.append(f"         {ctx.rstrip()}")
            lines.append("")

        if self.errors:
            lines.append("⚠️ 오류:")
            for err in self.errors:
                lines.append(f"   {err}")

        return "\n".join(lines)

    def to_json(self) -> str:
        """JSON 형식."""
        data = {
            "query": self.query,
            "total_matches": self.total_matches,
            "files_scanned": self.files_scanned,
            "files_matched": self.files_matched,
            "matches": [asdict(m) for m in self.matches],
        }
        if self.errors:
            data["errors"] = self.errors
        return json.dumps(data, ensure_ascii=False, indent=2)


# ============================================================
# 1. 파일 탐색 유틸리티
# ============================================================

# 검색 제외 디렉터리
SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", "dist", "build", ".eggs", ".mypy_cache",
    ".pytest_cache", ".next", ".nuxt", "coverage",
    ".idea", ".vscode", ".vs",
})

# 검색 대상 확장자
CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs",
    ".java", ".kt", ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".swift", ".rb", ".php", ".scala", ".dart",
    ".sh", ".bash", ".zsh", ".ps1",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".txt", ".rst",
    ".html", ".css", ".scss", ".less",
    ".sql", ".graphql",
    ".dockerfile", ".xml",
})

# 바이너리/거대 파일 제외 기준
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MiB
MAX_RESULTS = 200  # 최대 결과 수


def _iter_code_files(directory: str) -> List[str]:
    """코드 파일 목록 수집 (재귀, 제외 규칙 적용)."""
    abs_dir = os.path.abspath(directory)
    result = []

    for root, dirs, files in os.walk(abs_dir):
        # 제외 디렉터리 필터링
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in CODE_EXTENSIONS:
                # 확장자 없는 특수 파일 허용 (Dockerfile, Makefile 등)
                if fname not in ("Dockerfile", "Makefile", "Rakefile", "Gemfile", "Procfile"):
                    continue

            fpath = os.path.join(root, fname)
            try:
                size = os.path.getsize(fpath)
                if size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            result.append(fpath)

    return result


def _read_file_safe(filepath: str) -> Optional[Tuple[str, List[str]]]:
    """파일 안전 읽기. (content, lines) 반환."""
    for enc in ("utf-8", "utf-8-sig", "euc-kr", "cp949", "latin-1"):
        try:
            with open(filepath, "r", encoding=enc) as f:
                content = f.read()
            return content, content.splitlines()
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:
            return None
    return None


# ============================================================
# 2. CodeSearcher 메인 클래스
# ============================================================

class CodeSearcher:
    """코드 구조 검색 도구."""

    def __init__(self):
        self._eye = CodeEye() if CodeEye else None

    # --------------------------------------------------------
    # 2-1. 심볼 검색 (함수/클래스/메서드 이름)
    # --------------------------------------------------------
    def search_symbols(self, directory: str, pattern: str) -> SearchResult:
        """
        심볼(함수/클래스/메서드) 이름 검색.
        code_eye 모듈의 outline 기능을 활용한 구조적 검색.
        """
        result = SearchResult(query=f"symbol:{pattern}")
        abs_dir = os.path.abspath(directory)

        try:
            pat = re.compile(pattern, re.IGNORECASE)
        except re.error:
            pat = re.compile(re.escape(pattern), re.IGNORECASE)

        files = _iter_code_files(directory)
        result.files_scanned = len(files)

        for fpath in files:
            if result.total_matches >= MAX_RESULTS:
                break

            try:
                if self._eye:
                    outline = self._eye.outline(fpath)
                    for sym in outline.symbols:
                        if pat.search(sym.name) or pat.search(sym.signature):
                            rel_path = os.path.relpath(fpath, abs_dir)
                            file_data = _read_file_safe(fpath)
                            line_content = ""
                            if file_data:
                                _, lines = file_data
                                if 0 < sym.line_start <= len(lines):
                                    line_content = lines[sym.line_start - 1]

                            result.matches.append(SearchMatch(
                                filepath=fpath,
                                rel_path=rel_path,
                                line_number=sym.line_start,
                                line_content=line_content,
                                symbol_name=sym.name,
                                symbol_kind=sym.kind,
                                match_type="definition",
                            ))
                            result.total_matches += 1

                            if result.total_matches >= MAX_RESULTS:
                                break
                else:
                    # code_eye 없으면 정규식 폴백
                    self._symbol_search_regex(fpath, abs_dir, pat, result)

            except Exception as e:
                result.errors.append(f"{fpath}: {e}")

        result.files_matched = len(set(m.rel_path for m in result.matches))
        return result

    def _symbol_search_regex(self, fpath: str, abs_dir: str, pat: re.Pattern, result: SearchResult):
        """정규식 기반 심볼 검색 (code_eye 폴백)."""
        file_data = _read_file_safe(fpath)
        if not file_data:
            return

        content, lines = file_data
        rel_path = os.path.relpath(fpath, abs_dir)

        # 심볼 정의 패턴
        sym_patterns = [
            ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function|func|fn)\s+(\w+)", re.MULTILINE)),
            ("method", re.compile(r"^\s+(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)),
        ]

        for kind, sp in sym_patterns:
            for m in sp.finditer(content):
                name = m.group(1)
                if pat.search(name):
                    line_no = content[:m.start()].count("\n") + 1
                    line_content = lines[line_no - 1] if line_no <= len(lines) else ""
                    result.matches.append(SearchMatch(
                        filepath=fpath,
                        rel_path=rel_path,
                        line_number=line_no,
                        line_content=line_content,
                        symbol_name=name,
                        symbol_kind=kind,
                        match_type="definition",
                    ))
                    result.total_matches += 1

    # --------------------------------------------------------
    # 2-2. 참조 검색 (심볼 사용/호출 위치)
    # --------------------------------------------------------
    def search_references(self, directory: str, symbol: str, context_lines: int = 2) -> SearchResult:
        """심볼의 참조(호출/사용) 위치 검색."""
        result = SearchResult(query=f"ref:{symbol}")
        abs_dir = os.path.abspath(directory)

        # 단어 경계 매칭 (정의가 아닌 사용처)
        try:
            pat = re.compile(r"\b" + re.escape(symbol) + r"\b")
        except re.error:
            pat = re.compile(re.escape(symbol))

        # 정의 패턴 (제외용)
        def_patterns = [
            re.compile(r"^\s*(?:def|function|func|fn|class|interface|struct|type|enum)\s+" + re.escape(symbol) + r"\b"),
            re.compile(r"^\s*(?:import|from)\s+.*" + re.escape(symbol)),
        ]

        files = _iter_code_files(directory)
        result.files_scanned = len(files)

        for fpath in files:
            if result.total_matches >= MAX_RESULTS:
                break

            file_data = _read_file_safe(fpath)
            if not file_data:
                continue

            content, lines = file_data
            rel_path = os.path.relpath(fpath, abs_dir)

            for i, line in enumerate(lines):
                if result.total_matches >= MAX_RESULTS:
                    break

                if not pat.search(line):
                    continue

                # 정의 줄은 제외
                is_def = any(dp.match(line) for dp in def_patterns)
                if is_def:
                    continue

                # 주석 줄 제외 (간이)
                stripped = line.lstrip()
                if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
                    match_type = "comment_ref"
                else:
                    match_type = "reference"

                ctx_before = [lines[j] for j in range(max(0, i - context_lines), i)]
                ctx_after = [lines[j] for j in range(i + 1, min(len(lines), i + 1 + context_lines))]

                result.matches.append(SearchMatch(
                    filepath=fpath,
                    rel_path=rel_path,
                    line_number=i + 1,
                    line_content=line,
                    context_before=ctx_before,
                    context_after=ctx_after,
                    symbol_name=symbol,
                    match_type=match_type,
                ))
                result.total_matches += 1

        result.files_matched = len(set(m.rel_path for m in result.matches))
        return result

    # --------------------------------------------------------
    # 2-3. 내용 검색 (텍스트 + 컨텍스트)
    # --------------------------------------------------------
    def search_content(self, directory: str, query: str, context_lines: int = 3) -> SearchResult:
        """내용(텍스트) 검색 + 전후 컨텍스트."""
        result = SearchResult(query=query)
        abs_dir = os.path.abspath(directory)

        try:
            pat = re.compile(query, re.IGNORECASE)
        except re.error:
            pat = re.compile(re.escape(query), re.IGNORECASE)

        files = _iter_code_files(directory)
        result.files_scanned = len(files)

        for fpath in files:
            if result.total_matches >= MAX_RESULTS:
                break

            file_data = _read_file_safe(fpath)
            if not file_data:
                continue

            content, lines = file_data
            rel_path = os.path.relpath(fpath, abs_dir)

            for i, line in enumerate(lines):
                if result.total_matches >= MAX_RESULTS:
                    break

                if not pat.search(line):
                    continue

                ctx_before = [lines[j] for j in range(max(0, i - context_lines), i)]
                ctx_after = [lines[j] for j in range(i + 1, min(len(lines), i + 1 + context_lines))]

                result.matches.append(SearchMatch(
                    filepath=fpath,
                    rel_path=rel_path,
                    line_number=i + 1,
                    line_content=line,
                    context_before=ctx_before,
                    context_after=ctx_after,
                    match_type="content",
                ))
                result.total_matches += 1

        result.files_matched = len(set(m.rel_path for m in result.matches))
        return result

    # --------------------------------------------------------
    # 2-4. 구조 검색 (종류별)
    # --------------------------------------------------------
    def search_structure(self, directory: str, kind: str = "all") -> SearchResult:
        """종류별 구조 검색 (class, function, method 등)."""
        result = SearchResult(query=f"structure:{kind}")
        abs_dir = os.path.abspath(directory)
        kind_lower = kind.lower()

        files = _iter_code_files(directory)
        result.files_scanned = len(files)

        for fpath in files:
            if result.total_matches >= MAX_RESULTS:
                break

            try:
                if self._eye:
                    outline = self._eye.outline(fpath)
                    for sym in outline.symbols:
                        if kind_lower == "all" or sym.kind == kind_lower:
                            rel_path = os.path.relpath(fpath, abs_dir)
                            file_data = _read_file_safe(fpath)
                            line_content = ""
                            if file_data:
                                _, lines = file_data
                                if 0 < sym.line_start <= len(lines):
                                    line_content = lines[sym.line_start - 1]

                            result.matches.append(SearchMatch(
                                filepath=fpath,
                                rel_path=os.path.relpath(fpath, abs_dir),
                                line_number=sym.line_start,
                                line_content=line_content,
                                symbol_name=sym.name,
                                symbol_kind=sym.kind,
                                match_type="structure",
                            ))
                            result.total_matches += 1

                            if result.total_matches >= MAX_RESULTS:
                                break
            except Exception as e:
                result.errors.append(f"{fpath}: {e}")

        result.files_matched = len(set(m.rel_path for m in result.matches))
        return result

    # --------------------------------------------------------
    # 2-5. 파일명 검색
    # --------------------------------------------------------
    def search_files(self, directory: str, pattern: str) -> SearchResult:
        """파일명 패턴 검색."""
        result = SearchResult(query=f"file:{pattern}")
        abs_dir = os.path.abspath(directory)

        try:
            pat = re.compile(pattern, re.IGNORECASE)
        except re.error:
            pat = re.compile(re.escape(pattern), re.IGNORECASE)

        for root, dirs, files in os.walk(abs_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

            for fname in files:
                if pat.search(fname):
                    fpath = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, abs_dir)
                    try:
                        size = os.path.getsize(fpath)
                    except OSError:
                        size = 0

                    result.matches.append(SearchMatch(
                        filepath=fpath,
                        rel_path=rel_path,
                        line_number=0,
                        line_content=f"[{_human_bytes(size)}]",
                        match_type="file",
                    ))
                    result.total_matches += 1
                    result.files_scanned += 1

                    if result.total_matches >= MAX_RESULTS:
                        break

        result.files_matched = result.total_matches
        return result

    # --------------------------------------------------------
    # 2-6. TODO/FIXME/HACK 검색
    # --------------------------------------------------------
    def search_todo(self, directory: str) -> SearchResult:
        """TODO/FIXME/HACK 코멘트 검색."""
        pat = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG|OPTIMIZE|REFACTOR)\b", re.IGNORECASE)
        result = SearchResult(query="TODO/FIXME/HACK")
        abs_dir = os.path.abspath(directory)

        files = _iter_code_files(directory)
        result.files_scanned = len(files)

        for fpath in files:
            if result.total_matches >= MAX_RESULTS:
                break

            file_data = _read_file_safe(fpath)
            if not file_data:
                continue

            content, lines = file_data
            rel_path = os.path.relpath(fpath, abs_dir)

            for i, line in enumerate(lines):
                m = pat.search(line)
                if m:
                    result.matches.append(SearchMatch(
                        filepath=fpath,
                        rel_path=rel_path,
                        line_number=i + 1,
                        line_content=line,
                        symbol_name=m.group(1).upper(),
                        match_type="todo",
                    ))
                    result.total_matches += 1

                    if result.total_matches >= MAX_RESULTS:
                        break

        result.files_matched = len(set(m.rel_path for m in result.matches))
        return result


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ============================================================
# 3. CLI 진입점
# ============================================================

def _cli_main():
    import argparse

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="search_code.py — 코드 구조 검색 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="검색 명령")

    # symbols
    p_sym = sub.add_parser("symbols", help="심볼(함수/클래스) 이름 검색")
    p_sym.add_argument("directory", help="검색 디렉터리")
    p_sym.add_argument("pattern", help="검색 패턴 (정규식)")

    # references
    p_ref = sub.add_parser("references", help="심볼 참조 위치 검색")
    p_ref.add_argument("directory", help="검색 디렉터리")
    p_ref.add_argument("symbol", help="심볼 이름")
    p_ref.add_argument("--context", "-c", type=int, default=2, help="컨텍스트 줄 수")

    # content
    p_cnt = sub.add_parser("content", help="내용 검색")
    p_cnt.add_argument("directory", help="검색 디렉터리")
    p_cnt.add_argument("query", help="검색어 (정규식)")
    p_cnt.add_argument("--context", "-c", type=int, default=3, help="컨텍스트 줄 수")

    # structure
    p_str = sub.add_parser("structure", help="구조 검색")
    p_str.add_argument("directory", help="검색 디렉터리")
    p_str.add_argument("--kind", "-k", default="all",
                        choices=["all", "class", "function", "method", "variable", "constant"],
                        help="심볼 종류")

    # files
    p_file = sub.add_parser("files", help="파일명 검색")
    p_file.add_argument("directory", help="검색 디렉터리")
    p_file.add_argument("pattern", help="파일명 패턴 (정규식)")

    # todo
    p_todo = sub.add_parser("todo", help="TODO/FIXME/HACK 검색")
    p_todo.add_argument("directory", help="검색 디렉터리")

    # 공통 옵션
    parser.add_argument("--json", action="store_true", help="JSON 형식 출력")

    args = parser.parse_args()
    searcher = CodeSearcher()
    use_json = getattr(args, "json", False)

    if args.command == "symbols":
        r = searcher.search_symbols(args.directory, args.pattern)
    elif args.command == "references":
        r = searcher.search_references(args.directory, args.symbol, args.context)
    elif args.command == "content":
        r = searcher.search_content(args.directory, args.query, args.context)
    elif args.command == "structure":
        r = searcher.search_structure(args.directory, args.kind)
    elif args.command == "files":
        r = searcher.search_files(args.directory, args.pattern)
    elif args.command == "todo":
        r = searcher.search_todo(args.directory)
    else:
        parser.print_help()
        return

    print(r.to_json() if use_json else r.to_text())


if __name__ == "__main__":
    _cli_main()
