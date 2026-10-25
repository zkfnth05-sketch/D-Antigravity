#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code_eye.py — 딥시크의 눈: 대형 파일 스마트 뷰어
================================================================================
[목적] 100K 글자 절단(truncation) 없이 대형 소스코드 파일을 지능적으로 분석.
       파일 전체 구조(개요)를 먼저 보여주고, 관련 구간만 정밀 조회할 수 있게 한다.

기능:
  1. outline(filepath)           — 파일 구조 개요 (클래스, 함수, import, 상수 등)
  2. view_section(filepath, name) — 특정 클래스/함수 섹션 정밀 조회
  3. smart_view(filepath, query)  — 개요 + 쿼리 관련 섹션 자동 표시
  4. search_symbols(directory, pattern) — 디렉터리 전체 심볼 검색

지원 언어:
  - Python (.py): ast 모듈 기반 정밀 파싱
  - JavaScript/TypeScript (.js/.ts/.jsx/.tsx): 정규식 기반 파싱
  - 기타: 범용 정규식 파싱 (def/function/class/struct/interface 등)

사용법 (CLI):
  python code_eye.py outline <filepath>
  python code_eye.py view_section <filepath> <section_name>
  python code_eye.py smart_view <filepath> [--query "검색어"]
  python code_eye.py search <directory> <pattern>

사용법 (모듈):
  from code_eye import CodeEye
  eye = CodeEye()
  outline = eye.outline("path/to/file.py")
  section = eye.view_section("path/to/file.py", "MyClass.my_method")

작성: Gemini (대표님 승인 하 단독 구현)
================================================================================
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# 0. 데이터 구조
# ============================================================

@dataclass
class Symbol:
    """소스코드 심볼 (함수, 클래스, 메서드, 변수, import 등)."""
    kind: str          # "class", "function", "method", "import", "constant", "variable", "decorator"
    name: str          # 심볼 이름 (예: "MyClass", "my_function", "MyClass.my_method")
    line_start: int    # 시작 줄 번호 (1-indexed)
    line_end: int      # 끝 줄 번호 (1-indexed)
    indent: int = 0    # 들여쓰기 수준
    parent: str = ""   # 부모 심볼 이름 (메서드의 경우 클래스명)
    signature: str = "" # 시그니처 요약 (예: "def foo(a, b) -> int")
    docstring: str = "" # 첫 줄 독스트링
    children: List[str] = field(default_factory=list)  # 자식 심볼 이름 목록

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if not d["children"]:
            del d["children"]
        if not d["parent"]:
            del d["parent"]
        if not d["docstring"]:
            del d["docstring"]
        return d


@dataclass
class FileOutline:
    """파일 구조 개요."""
    filepath: str
    language: str
    total_lines: int
    total_bytes: int
    encoding: str = "utf-8"
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary_text(self) -> str:
        """사람이 읽기 좋은 개요 텍스트 생성."""
        lines = []
        lines.append(f"📄 {os.path.basename(self.filepath)}")
        lines.append(f"   언어: {self.language} | {self.total_lines}줄 | {_human_bytes(self.total_bytes)}")
        lines.append("")

        if self.imports:
            lines.append(f"📦 Imports ({len(self.imports)}개):")
            for imp in self.imports[:20]:  # 최대 20개만 표시
                lines.append(f"   {imp}")
            if len(self.imports) > 20:
                lines.append(f"   ... 외 {len(self.imports) - 20}개")
            lines.append("")

        # 최상위 심볼만 트리 형태로 표시
        top_symbols = [s for s in self.symbols if not s.parent]
        if top_symbols:
            lines.append(f"🏗️ 구조 ({len(top_symbols)}개 최상위 심볼):")
            for sym in top_symbols:
                icon = _kind_icon(sym.kind)
                span = f"L{sym.line_start}-{sym.line_end}"
                sig = f" — {sym.signature}" if sym.signature else ""
                doc = f"  # {sym.docstring}" if sym.docstring else ""
                lines.append(f"   {icon} {sym.name} [{span}]{sig}{doc}")

                # 자식 심볼
                children = [s for s in self.symbols if s.parent == sym.name]
                for child in children:
                    c_icon = _kind_icon(child.kind)
                    c_span = f"L{child.line_start}-{child.line_end}"
                    c_sig = f" — {child.signature}" if child.signature else ""
                    c_doc = f"  # {child.docstring}" if child.docstring else ""
                    lines.append(f"      {c_icon} {child.name} [{c_span}]{c_sig}{c_doc}")

            lines.append("")

        if self.errors:
            lines.append("⚠️ 파싱 경고:")
            for err in self.errors:
                lines.append(f"   {err}")

        return "\n".join(lines)


def _kind_icon(kind: str) -> str:
    return {
        "class": "🔷",
        "function": "🔹",
        "method": "🔸",
        "import": "📦",
        "constant": "🔒",
        "variable": "📌",
        "decorator": "🏷️",
        "interface": "🔶",
        "struct": "🔶",
        "enum": "🔶",
        "type": "🔶",
    }.get(kind, "•")


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ============================================================
# 1. Python 파서 (ast 기반)
# ============================================================

class PythonParser:
    """Python 소스코드를 ast 모듈로 정밀 파싱."""

    @staticmethod
    def parse(source: str, filepath: str) -> FileOutline:
        lines = source.splitlines()
        total_lines = len(lines)
        total_bytes = len(source.encode("utf-8", errors="replace"))

        outline = FileOutline(
            filepath=filepath,
            language="Python",
            total_lines=total_lines,
            total_bytes=total_bytes,
        )

        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError as e:
            outline.errors.append(f"SyntaxError: {e}")
            # 폴백: 정규식 파싱
            return GenericParser.parse(source, filepath, language="Python (fallback)")

        # Import 추출
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    outline.imports.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    outline.imports.append(
                        f"from {module} import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                    )

        # 최상위 심볼 추출
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                sym = PythonParser._parse_class(node, lines)
                outline.symbols.append(sym)
                # 메서드 추출
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method = PythonParser._parse_function(child, lines, parent=node.name)
                        method.kind = "method"
                        method.name = f"{node.name}.{child.name}"
                        outline.symbols.append(method)
                        sym.children.append(method.name)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym = PythonParser._parse_function(node, lines)
                outline.symbols.append(sym)

            elif isinstance(node, ast.Assign):
                # 모듈 레벨 상수/변수
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        kind = "constant" if name.isupper() else "variable"
                        outline.symbols.append(Symbol(
                            kind=kind,
                            name=name,
                            line_start=node.lineno,
                            line_end=node.end_lineno or node.lineno,
                        ))

        return outline

    @staticmethod
    def _parse_class(node: ast.ClassDef, lines: List[str]) -> Symbol:
        bases = ", ".join(
            ast.dump(b) if not isinstance(b, ast.Name) else b.id
            for b in node.bases
        )
        sig = f"class {node.name}" + (f"({bases})" if bases else "")
        doc = ast.get_docstring(node) or ""
        first_doc_line = doc.split("\n")[0].strip()[:100] if doc else ""

        return Symbol(
            kind="class",
            name=node.name,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            signature=sig,
            docstring=first_doc_line,
        )

    @staticmethod
    def _parse_function(node, lines: List[str], parent: str = "") -> Symbol:
        # 함수 시그니처 재구성
        args_parts = []
        for arg in node.args.args:
            arg_name = arg.arg
            if arg.annotation:
                try:
                    ann = ast.unparse(arg.annotation)
                except Exception:
                    ann = "?"
                arg_name = f"{arg_name}: {ann}"
            args_parts.append(arg_name)

        # *args, **kwargs
        if node.args.vararg:
            args_parts.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            args_parts.append(f"**{node.args.kwarg.arg}")

        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        returns = ""
        if node.returns:
            try:
                returns = f" -> {ast.unparse(node.returns)}"
            except Exception:
                returns = " -> ?"

        sig = f"{prefix} {node.name}({', '.join(args_parts)}){returns}"

        doc = ast.get_docstring(node) or ""
        first_doc_line = doc.split("\n")[0].strip()[:100] if doc else ""

        return Symbol(
            kind="function",
            name=node.name,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            parent=parent,
            signature=sig,
            docstring=first_doc_line,
        )


# ============================================================
# 2. 범용 파서 (정규식 기반)
# ============================================================

# 각 언어별 심볼 패턴
_LANG_PATTERNS: Dict[str, List[Tuple[str, re.Pattern]]] = {
    "javascript": [
        ("class", re.compile(r"^(\s*)(?:export\s+)?class\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE)),
        ("function", re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE)),
        ("function", re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function", re.MULTILINE)),
        ("interface", re.compile(r"^(\s*)(?:export\s+)?interface\s+(\w+)", re.MULTILINE)),
        ("type", re.compile(r"^(\s*)(?:export\s+)?type\s+(\w+)", re.MULTILINE)),
        ("enum", re.compile(r"^(\s*)(?:export\s+)?enum\s+(\w+)", re.MULTILINE)),
    ],
    "java": [
        ("class", re.compile(r"^(\s*)(?:public|private|protected)?\s*(?:static\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^(\s*)(?:public|private|protected)?\s*interface\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^(\s*)(?:public|private|protected)?\s*(?:static\s+)?(?:\w+(?:<[^>]+>)?)\s+(\w+)\s*\(", re.MULTILINE)),
    ],
    "go": [
        ("function", re.compile(r"^func\s+(?:\([^)]*\)\s+)?(\w+)\s*\(", re.MULTILINE)),
        ("struct", re.compile(r"^type\s+(\w+)\s+struct\s*\{", re.MULTILINE)),
        ("interface", re.compile(r"^type\s+(\w+)\s+interface\s*\{", re.MULTILINE)),
    ],
    "rust": [
        ("function", re.compile(r"^(\s*)(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)),
        ("struct", re.compile(r"^(\s*)(?:pub\s+)?struct\s+(\w+)", re.MULTILINE)),
        ("enum", re.compile(r"^(\s*)(?:pub\s+)?enum\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^(\s*)(?:pub\s+)?trait\s+(\w+)", re.MULTILINE)),
    ],
    "generic": [
        ("class", re.compile(r"^(\s*)(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?(?:def|function|func|fn)\s+(\w+)", re.MULTILINE)),
        ("struct", re.compile(r"^(\s*)(?:pub\s+)?(?:type\s+)?struct\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^(\s*)interface\s+(\w+)", re.MULTILINE)),
    ],
}

# 확장자 → 언어 매핑
_EXT_LANG: Dict[str, str] = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "javascript", ".tsx": "javascript",
    ".java": "java", ".kt": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "generic", ".cpp": "generic", ".h": "generic", ".hpp": "generic",
    ".cs": "generic", ".swift": "generic", ".rb": "generic",
    ".php": "generic", ".scala": "generic", ".dart": "generic",
}

# Import 패턴
_IMPORT_PATTERNS = [
    re.compile(r"^(?:import|from)\s+(.+?)(?:\s+import.*)?$", re.MULTILINE),        # Python
    re.compile(r"^(?:import|const|let|var)\s+.*(?:require|from)\s*[('\"](.+?)['\")]", re.MULTILINE),  # JS
    re.compile(r"^import\s+\"(.+?)\"", re.MULTILINE),                              # Go
    re.compile(r"^use\s+(.+?);", re.MULTILINE),                                     # Rust
    re.compile(r"^import\s+(.+?)(?:;|\s*$)", re.MULTILINE),                         # Java
]


class GenericParser:
    """정규식 기반 범용 파서."""

    @staticmethod
    def parse(source: str, filepath: str, language: str = "") -> FileOutline:
        lines = source.splitlines()
        total_lines = len(lines)
        total_bytes = len(source.encode("utf-8", errors="replace"))

        ext = os.path.splitext(filepath)[1].lower()
        if not language:
            language = _EXT_LANG.get(ext, "Unknown") or ext

        lang_key = _EXT_LANG.get(ext, "generic")
        patterns = _LANG_PATTERNS.get(lang_key, _LANG_PATTERNS["generic"])

        outline = FileOutline(
            filepath=filepath,
            language=language,
            total_lines=total_lines,
            total_bytes=total_bytes,
        )

        # Import 추출
        for pat in _IMPORT_PATTERNS:
            for m in pat.finditer(source):
                imp_text = m.group(0).strip()
                if imp_text and imp_text not in outline.imports:
                    outline.imports.append(imp_text)

        # 심볼 추출
        found_symbols: List[Tuple[int, Symbol]] = []
        for kind, pat in patterns:
            for m in pat.finditer(source):
                line_no = source[:m.start()].count("\n") + 1
                groups = m.groups()
                if len(groups) >= 2:
                    indent_str, name = groups[0], groups[1]
                    indent = len(indent_str) if indent_str else 0
                elif len(groups) == 1:
                    name = groups[0]
                    indent = 0
                else:
                    continue

                # 줄 끝 추정 (다음 같은/낮은 들여쓰기 줄까지)
                line_end = GenericParser._estimate_end(lines, line_no - 1, indent)

                sig_line = lines[line_no - 1].strip() if line_no <= total_lines else ""
                found_symbols.append((line_no, Symbol(
                    kind=kind,
                    name=name,
                    line_start=line_no,
                    line_end=line_end,
                    indent=indent,
                    signature=sig_line[:120],
                )))

        # 줄 번호 순 정렬 & 중복 제거
        found_symbols.sort(key=lambda x: x[0])
        seen_names: set = set()
        for _, sym in found_symbols:
            key = f"{sym.name}:{sym.line_start}"
            if key not in seen_names:
                seen_names.add(key)
                outline.symbols.append(sym)

        return outline

    @staticmethod
    def _estimate_end(lines: List[str], start_idx: int, base_indent: int) -> int:
        """블록 끝 줄 추정 (들여쓰기 기반)."""
        total = len(lines)
        if start_idx >= total:
            return total

        # 시작 줄 다음부터 탐색
        for i in range(start_idx + 1, min(start_idx + 2000, total)):
            line = lines[i]
            stripped = line.lstrip()
            if not stripped:  # 빈 줄은 건너뛰기
                continue
            current_indent = len(line) - len(stripped)
            # 같은 또는 더 낮은 들여쓰기 → 이전 줄이 끝
            if current_indent <= base_indent and stripped:
                # 이전의 비-빈 줄 찾기
                for j in range(i - 1, start_idx, -1):
                    if lines[j].strip():
                        return j + 1  # 1-indexed
                return i  # 1-indexed

        return total  # 파일 끝까지


# ============================================================
# 3. CodeEye 메인 클래스
# ============================================================

class CodeEye:
    """대형 파일 스마트 뷰어 — 100K 절단 없이 구조적으로 분석."""

    @staticmethod
    def read_file(filepath: str) -> Tuple[str, str]:
        """
        파일을 전량 읽기 (절단 없음).

        Returns:
            (content, encoding)
        """
        abs_path = os.path.abspath(filepath)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"파일 없음: {abs_path}")

        # 인코딩 자동 감지
        for enc in ("utf-8", "utf-8-sig", "euc-kr", "cp949", "latin-1"):
            try:
                with open(abs_path, "r", encoding=enc) as f:
                    content = f.read()  # 전량 읽기, 절단 없음!
                return content, enc
            except (UnicodeDecodeError, UnicodeError):
                continue

        # 최종 폴백: 바이너리로 읽어서 에러 무시
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return content, "utf-8 (lossy)"

    def outline(self, filepath: str) -> FileOutline:
        """파일 구조 개요 생성."""
        content, encoding = self.read_file(filepath)
        ext = os.path.splitext(filepath)[1].lower()

        if ext == ".py":
            outline = PythonParser.parse(content, filepath)
        else:
            outline = GenericParser.parse(content, filepath)

        outline.encoding = encoding
        return outline

    def view_section(self, filepath: str, section_name: str) -> str:
        """
        특정 섹션(클래스/함수) 소스코드 조회.

        Args:
            filepath: 파일 경로
            section_name: 심볼 이름 (예: "MyClass", "my_function", "MyClass.my_method")

        Returns:
            해당 섹션의 소스코드 (줄 번호 포함)
        """
        content, _ = self.read_file(filepath)
        lines = content.splitlines()
        outline = self.outline(filepath)

        # 심볼 검색 (대소문자 무시, 부분 일치)
        target = section_name.lower()
        matching: List[Symbol] = []
        for sym in outline.symbols:
            if target == sym.name.lower():
                matching.append(sym)
            elif target in sym.name.lower():
                matching.append(sym)

        if not matching:
            return f"❌ 심볼 '{section_name}' 을 찾을 수 없습니다.\n\n사용 가능한 심볼:\n" + \
                   "\n".join(f"  - {s.name} ({s.kind}, L{s.line_start}-{s.line_end})" for s in outline.symbols)

        result_parts = []
        for sym in matching:
            start = max(0, sym.line_start - 1)
            end = min(len(lines), sym.line_end)
            section_lines = []
            for i in range(start, end):
                section_lines.append(f"{i + 1:>6}: {lines[i]}")
            result_parts.append(
                f"{'─' * 60}\n"
                f"{_kind_icon(sym.kind)} {sym.name} ({sym.kind}) [L{sym.line_start}-{sym.line_end}]\n"
                f"{'─' * 60}\n" +
                "\n".join(section_lines)
            )

        return "\n\n".join(result_parts)

    def view_lines(self, filepath: str, line_start: int, line_end: int) -> str:
        """
        줄 번호 범위로 파일 구간 직접 조회 (절단 없음).

        Args:
            filepath: 파일 경로
            line_start: 시작 줄 번호 (1-indexed)
            line_end: 끝 줄 번호 (1-indexed, 포함)

        Returns:
            해당 구간의 소스코드 (줄 번호 포함)
        """
        content, _ = self.read_file(filepath)
        lines = content.splitlines()
        total = len(lines)

        # 범위 클램핑
        s = max(1, line_start)
        e = min(total, line_end)
        if s > e:
            return f"❌ 유효하지 않은 줄 범위: {line_start}-{line_end} (파일 총 {total}줄)"

        section_lines = []
        for i in range(s - 1, e):
            section_lines.append(f"{i + 1:>6}: {lines[i]}")
        return (
            f"{'─' * 60}\n"
            f"📄 {os.path.basename(filepath)} [L{s}-{e}] (총 {total}줄)\n"
            f"{'─' * 60}\n" +
            "\n".join(section_lines)
        )

    def smart_view(self, filepath: str, query: Optional[str] = None) -> str:
        """
        스마트 뷰: 개요 + 관련 섹션 자동 표시.

        query가 없으면 개요만 표시.
        query가 있으면 개요 + 관련 심볼 소스코드 표시.
        """
        outline = self.outline(filepath)
        result_parts = [outline.summary_text()]

        if query:
            content, _ = self.read_file(filepath)
            lines = content.splitlines()
            query_lower = query.lower()

            # 1) 심볼 이름 매칭
            matching_symbols = [
                s for s in outline.symbols
                if query_lower in s.name.lower() or query_lower in s.signature.lower()
            ]

            # 2) 내용 매칭 (심볼 이름으로 못 찾은 경우)
            if not matching_symbols:
                for sym in outline.symbols:
                    start = max(0, sym.line_start - 1)
                    end = min(len(lines), sym.line_end)
                    section_text = "\n".join(lines[start:end]).lower()
                    if query_lower in section_text:
                        matching_symbols.append(sym)

            if matching_symbols:
                result_parts.append(f"\n🔍 '{query}' 관련 섹션 ({len(matching_symbols)}개):")
                for sym in matching_symbols[:10]:  # 최대 10개
                    start = max(0, sym.line_start - 1)
                    end = min(len(lines), sym.line_end)
                    section_lines = []
                    for i in range(start, end):
                        section_lines.append(f"{i + 1:>6}: {lines[i]}")
                    result_parts.append(
                        f"\n{'─' * 60}\n"
                        f"{_kind_icon(sym.kind)} {sym.name} [{sym.kind}] L{sym.line_start}-{sym.line_end}\n"
                        f"{'─' * 60}\n" +
                        "\n".join(section_lines)
                    )
                if len(matching_symbols) > 10:
                    result_parts.append(f"\n... 외 {len(matching_symbols) - 10}개 심볼")
            else:
                # 줄 단위 매칭 폴백
                matched_lines = []
                for i, line in enumerate(lines):
                    if query_lower in line.lower():
                        matched_lines.append(i)

                if matched_lines:
                    result_parts.append(f"\n🔍 '{query}' 텍스트 매칭 ({len(matched_lines)}건):")
                    for idx in matched_lines[:30]:
                        # 전후 2줄 컨텍스트
                        ctx_start = max(0, idx - 2)
                        ctx_end = min(len(lines), idx + 3)
                        for i in range(ctx_start, ctx_end):
                            marker = "→" if i == idx else " "
                            result_parts.append(f"  {marker} {i + 1:>6}: {lines[i]}")
                        result_parts.append("")
                else:
                    result_parts.append(f"\n❌ '{query}'에 대한 매칭 결과 없음")

        return "\n".join(result_parts)

    def search_symbols(self, directory: str, pattern: str) -> str:
        """
        디렉터리 전체에서 심볼 검색.

        Args:
            directory: 검색 대상 디렉터리
            pattern: 검색 패턴 (정규식 지원)

        Returns:
            매칭된 심볼 목록 (파일별 그룹핑)
        """
        abs_dir = os.path.abspath(directory)
        if not os.path.isdir(abs_dir):
            return f"❌ 디렉터리 없음: {abs_dir}"

        try:
            pat = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # 정규식 실패 시 리터럴 검색
            pat = re.compile(re.escape(pattern), re.IGNORECASE)

        results: Dict[str, List[Symbol]] = {}
        files_scanned = 0
        skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", "dist", "build"}

        for root, dirs, files in os.walk(abs_dir):
            # 제외 디렉터리
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]

            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
                               ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php"):
                    continue

                fpath = os.path.join(root, fname)
                files_scanned += 1

                try:
                    outline = self.outline(fpath)
                    for sym in outline.symbols:
                        if pat.search(sym.name) or pat.search(sym.signature):
                            rel_path = os.path.relpath(fpath, abs_dir)
                            if rel_path not in results:
                                results[rel_path] = []
                            results[rel_path].append(sym)
                except Exception:
                    continue

        # 결과 포맷
        if not results:
            return f"❌ '{pattern}' 매칭 심볼 없음 ({files_scanned}개 파일 스캔)"

        lines = [f"🔍 '{pattern}' 검색 결과 ({sum(len(v) for v in results.values())}건, {files_scanned}개 파일 스캔):"]
        for rel_path, symbols in sorted(results.items()):
            lines.append(f"\n  📄 {rel_path}:")
            for sym in symbols:
                lines.append(f"     {_kind_icon(sym.kind)} {sym.name} [{sym.kind}] L{sym.line_start}-{sym.line_end}")
                if sym.signature:
                    lines.append(f"       {sym.signature[:100]}")

        return "\n".join(lines)


# ============================================================
# 4. CLI 진입점
# ============================================================

def _cli_main():
    import argparse

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="code_eye.py — 딥시크의 눈: 대형 파일 스마트 뷰어",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="실행할 명령")

    # outline
    p_outline = sub.add_parser("outline", help="파일 구조 개요")
    p_outline.add_argument("filepath", help="대상 파일 경로")
    p_outline.add_argument("--json", action="store_true", help="JSON 형식 출력")

    # view_section
    p_section = sub.add_parser("view_section", help="특정 섹션 조회")
    p_section.add_argument("filepath", help="대상 파일 경로")
    p_section.add_argument("name", help="심볼 이름 (예: MyClass, my_function)")

    # smart_view
    p_smart = sub.add_parser("smart_view", help="스마트 뷰 (개요 + 관련 섹션)")
    p_smart.add_argument("filepath", help="대상 파일 경로")
    p_smart.add_argument("--query", "-q", default=None, help="검색 쿼리")

    # search
    p_search = sub.add_parser("search", help="디렉터리 심볼 검색")
    p_search.add_argument("directory", help="검색 대상 디렉터리")
    p_search.add_argument("pattern", help="검색 패턴 (정규식 지원)")

    args = parser.parse_args()
    eye = CodeEye()

    if args.command == "outline":
        outline = eye.outline(args.filepath)
        if args.json:
            data = {
                "filepath": outline.filepath,
                "language": outline.language,
                "total_lines": outline.total_lines,
                "total_bytes": outline.total_bytes,
                "encoding": outline.encoding,
                "imports": outline.imports,
                "symbols": [s.to_dict() for s in outline.symbols],
                "errors": outline.errors,
            }
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(outline.summary_text())

    elif args.command == "view_section":
        print(eye.view_section(args.filepath, args.name))

    elif args.command == "smart_view":
        print(eye.smart_view(args.filepath, args.query))

    elif args.command == "search":
        print(eye.search_symbols(args.directory, args.pattern))

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli_main()
