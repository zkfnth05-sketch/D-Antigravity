# -*- coding: utf-8 -*-
"""
[보정 패치] 잔여 스냅샷 누락 해결 + MCP 타임라인/롤백 도구 2종 등록

[1] project_manager.py
    - _snapshot_before_save 에 force 파라미터 추가 (기본 True)
      → 수정 시마다 내용 무변경 스킵에 걸리지 않고 빠짐없이 스냅샷 기록
    - save_file_content 호출부에서 force=True 명시 전달

[2] mcp_server.py
    - get_tools_list() 에 deepseek_view_timeline / deepseek_rollback_file 등록
    - handle_call_tool() 디스패치 분기 추가
    - _tool_view_timeline / _tool_rollback_file 핸들러 구현
    - _tool_autonomous_codegen 배너에 스냅샷 생성 시각 + diff 정보 표시

모든 치환은 SEARCH/REPLACE 규격이며, SEARCH 미발견/중복 시 파일 기록을 중단한다.
"""
import io
import os
import sys

PM = os.path.join("backend", "core", "project_manager.py")
MCP = os.path.join("backend", "core", "mcp_server.py")

PM_PATCHES = []
MCP_PATCHES = []


def pm_patch(name, search, replace):
    PM_PATCHES.append((name, search, replace))


def mcp_patch(name, search, replace):
    MCP_PATCHES.append((name, search, replace))


# ======================================================================
# [1] project_manager.py
# ======================================================================

# 1-1) _snapshot_before_save 시그니처 + force 적용
pm_patch(
    "pm-snapshot-before-save-force",
    '''    def _snapshot_before_save(self, target: str, note: str = "") -> Optional[Any]:
        """
        파일 저장 직전 원본 상태를 스냅샷으로 보존하고 timeline.jsonl 에 기록한다.

        Args:
            target : 대상 파일 절대경로
            note   : 변경 요약 메모

        Returns:
            SnapshotRecord 또는 None (스킵/실패)

        원칙:
          - 스냅샷 실패는 파일 저장을 절대 방해하지 않는다 (예외 격리).
          - 내용이 직전 스냅샷과 동일하면 엔진이 자동으로 스킵한다.
        """
        engine = self._ensure_snapshot_engine()
        if engine is None:
            return None

        try:
            rec = engine.create_backup(target, note=note)
''',
    '''    def _snapshot_before_save(self, target: str, note: str = "",
                              force: bool = True) -> Optional[Any]:
        """
        파일 저장 직전 원본 상태를 스냅샷으로 보존하고 timeline.jsonl 에 기록한다.

        Args:
            target : 대상 파일 절대경로
            note   : 변경 요약 메모
            force  : True(기본) 면 내용 무변경이어도 스냅샷을 강제 기록한다.
                     수정 시마다 빠짐없이 pre-edit 스냅샷을 남기기 위한 기본값.

        Returns:
            SnapshotRecord 또는 None (스킵/실패)

        원칙:
          - 스냅샷 실패는 파일 저장을 절대 방해하지 않는다 (예외 격리).
          - force=True 이므로 엔진의 '내용 무변경 스킵' 로직이 개입하지 않는다.
            (동일 내용 재저장이라도 수정 시점 자체를 이력으로 남긴다)
        """
        engine = self._ensure_snapshot_engine()
        if engine is None:
            return None

        try:
            rec = engine.create_backup(target, note=note, force=bool(force))
''',
)

# 1-2) 호출부에서 force=True 명시
pm_patch(
    "pm-snapshot-before-save-callsite",
    '''            self._snapshot_before_save(target, note="save_file_content")''',
    '''            self._snapshot_before_save(target, note="save_file_content", force=True)''',
)


# ======================================================================
# [2] mcp_server.py
# ======================================================================

# 2-1) get_tools_list — 도구 2종 등록
mcp_patch(
    "mcp-tools-list-register",
    '''            {
                "name": "deepseek_switch_project",
                "description": "active_project를 지정한 경로로 즉시 전환하고 config.json에 영구 반영합니다. 이후 모든 도구가 해당 프로젝트 기준으로 동작합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "전환할 프로젝트 루트 디렉터리 경로 (절대 경로 권장)"
                        }
                    },
                    "required": ["project_path"]
                }
            }
        ]''',
    '''            {
                "name": "deepseek_switch_project",
                "description": "active_project를 지정한 경로로 즉시 전환하고 config.json에 영구 반영합니다. 이후 모든 도구가 해당 프로젝트 기준으로 동작합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "전환할 프로젝트 루트 디렉터리 경로 (절대 경로 권장)"
                        }
                    },
                    "required": ["project_path"]
                }
            },
            {
                "name": "deepseek_view_timeline",
                "description": "시간별 스냅샷 타임라인(timeline.jsonl 원장)을 조회합니다. file_path를 지정하면 해당 파일의 시간대별 변경 이력과 각 스냅샷의 unified diff를, 생략하면 활성 프로젝트 전체 변경 이력을 최신순으로 반환합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "조회할 대상 파일 경로 (생략 시 활성 프로젝트 전체 이력 조회)"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "최대 반환 건수 (기본 10, 최신순)",
                            "default": 10
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "deepseek_rollback_file",
                "description": "지정한 특정 시간대(target_time) 또는 특정 스냅샷(snapshot_id) 또는 직전 상태(previous)로 파일을 1초 만에 안전하게 롤백합니다. 롤백 직전 상태도 자동 보존되어 '롤백의 롤백'이 가능합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "롤백할 대상 파일 경로 (필수)"
                        },
                        "target_time": {
                            "type": "string",
                            "description": "복원 기준 시각 (ISO 8601 문자열 또는 epoch 초). 지정 시각 이전 상태로 복원합니다."
                        },
                        "snapshot_id": {
                            "type": "string",
                            "description": "복원할 특정 스냅샷 ID (timeline 조회로 확인 가능)"
                        },
                        "previous": {
                            "type": "boolean",
                            "description": "True 이면 직전 스냅샷 상태로 복원합니다.",
                            "default": False
                        }
                    },
                    "required": ["file_path"]
                }
            }
        ]''',
)

# 2-2) handle_call_tool 디스패치 분기 추가
mcp_patch(
    "mcp-dispatch-branches",
    '''        elif tool_name == "deepseek_switch_project":
            return await self._tool_switch_project(arg_str("project_path"))

        else:''',
    '''        elif tool_name == "deepseek_switch_project":
            return await self._tool_switch_project(arg_str("project_path"))

        elif tool_name == "deepseek_view_timeline":
            return await self._tool_view_timeline(arg_str("file_path"),
                                                  args.get("limit", 10))

        elif tool_name == "deepseek_rollback_file":
            return await self._tool_rollback_file(arg_str("file_path"),
                                                  arg_str("target_time"),
                                                  arg_str("snapshot_id"),
                                                  args.get("previous", False))

        else:''',
)

# 2-3) 핸들러 2종 구현 (도구 6 뒤, JSON-RPC 루프 앞에 삽입)
mcp_patch(
    "mcp-handlers-implementation",
    '''    # ------------------------------------------------------------------
    # JSON-RPC stdio 루프
    # ------------------------------------------------------------------
    def run_stdio(self):''',
    '''    # ------------------------------------------------------------------
    # 도구 7: 시간별 타임라인 조회 (신설)
    # ------------------------------------------------------------------
    async def _tool_view_timeline(self, file_path: str, limit: Any = 10) -> Dict[str, Any]:
        """
        시간별 스냅샷 타임라인을 조회한다.

        Args:
            file_path : 대상 파일 (빈 문자열이면 활성 프로젝트 전체 이력)
            limit     : 최대 반환 건수 (기본 10)

        Returns:
            MCP content 응답 dict
        """
        # 진입 즉시 프로젝트 자동 전환 (파일이 지정된 경우)
        active = self.bind_project_for(file_path) if file_path else self.pm.active_project

        # limit 정규화 (문자열/None/비정상 값 방어)
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 10
        if cap <= 0:
            cap = 10

        try:
            tl = self.pm.get_timeline(file_path or None, limit=cap)
        except Exception as e:
            self.log(f"[MCP] 타임라인 조회 예외: {e}")
            return {
                "content": [{"type": "text",
                             "text": f"❌ 타임라인 조회 실패: {e}"}],
                "isError": True
            }

        if not tl.get("success"):
            return {
                "content": [{
                    "type": "text",
                    "text": (f"❌ 타임라인 조회 실패\\n\\n"
                             f"- 사유: {tl.get('message', '알 수 없는 오류')}\\n"
                             f"- 활성 프로젝트: `{active}`")
                }],
                "isError": True
            }

        scope = f"`{tl.get('path')}`" if tl.get("path") else "활성 프로젝트 전체"
        lines = [
            "## 🕰️ [시간별 스냅샷 타임라인]",
            f"**🎯 활성 프로젝트**: `{active}`",
            f"**📂 조회 범위**: {scope}",
            f"**📊 집계**: 표시 {tl.get('count', 0)}건 / 전체 {tl.get('total', 0)}건 (최신순)",
            ""
        ]

        timeline = tl.get("timeline") or []
        if not timeline:
            lines.append("_기록된 스냅샷이 없습니다. 파일을 저장하면 자동으로 이력이 쌓입니다._")
            return {"content": [{"type": "text", "text": "\\n".join(lines)}]}

        for idx, rec in enumerate(timeline, 1):
            sid = rec.get("snapshot_id", "")
            created = rec.get("created_at", "")
            note = rec.get("note", "") or "-"
            size = rec.get("size", 0)
            target = os.path.basename(rec.get("target_path", "") or "")
            pre_rb = " · 🔙 롤백직전보존" if rec.get("is_pre_rollback") else ""

            lines.append(f"### {idx}. `{sid}`{pre_rb}")
            lines.append(f"- **파일**: `{target}`")
            lines.append(f"- **생성 시각**: {created}")
            lines.append(f"- **메모**: {note}")
            lines.append(f"- **크기**: {size}B")

            # 각 스냅샷의 직전 대비 diff 조회
            try:
                d = self.pm.get_diff(sid, rec.get("target_path", "") or None)
            except Exception as e:
                d = {"success": False, "message": str(e)}

            if d.get("success") and d.get("changed"):
                lines.append(
                    "- **변경량**: +%d / -%d (직전 스냅샷 `%s` 대비)"
                    % (d.get("added_lines", 0), d.get("removed_lines", 0),
                       d.get("from_snapshot_id", "") or "-")
                )
                diff_text = (d.get("diff") or "").strip()
                if diff_text:
                    # 대화 폭주 방지 — 최대 40줄까지만 표시
                    diff_lines = diff_text.splitlines()
                    if len(diff_lines) > 40:
                        diff_lines = diff_lines[:40] + ["... (이하 생략)"]
                    lines.append("```diff")
                    lines.extend(diff_lines)
                    lines.append("```")
            elif d.get("success"):
                lines.append("- **변경량**: 변경 없음 (최초 스냅샷 또는 동일 내용)")
            else:
                lines.append(f"- **변경량**: diff 조회 불가 ({d.get('message', '')})")
            lines.append("")

        lines.append("💡 특정 시점으로 되돌리려면 `deepseek_rollback_file` 에 "
                     "`snapshot_id` 또는 `target_time` 을 지정하십시오.")
        return {"content": [{"type": "text", "text": "\\n".join(lines)}]}

    # ------------------------------------------------------------------
    # 도구 8: 시간별/스냅샷 롤백 (신설)
    # ------------------------------------------------------------------
    async def _tool_rollback_file(self, file_path: str, target_time: str = "",
                                  snapshot_id: str = "", previous: Any = False) -> Dict[str, Any]:
        """
        파일을 특정 스냅샷 / 특정 시각 / 직전 상태로 안전하게 롤백한다.

        Args:
            file_path   : 롤백 대상 파일 (필수)
            target_time : 복원 기준 시각 (ISO 문자열 또는 epoch)
            snapshot_id : 복원할 스냅샷 ID
            previous    : True 면 직전 스냅샷으로 복원

        Returns:
            MCP content 응답 dict
        """
        if not file_path:
            return {
                "content": [{"type": "text",
                             "text": "❌ file_path 인자가 비어 있습니다. 롤백할 파일 경로를 지정하십시오."}],
                "isError": True
            }

        # previous 정규화 (bool / "true" / "1" 등 수용)
        if isinstance(previous, str):
            prev_flag = previous.strip().lower() in ("1", "true", "yes", "y", "on")
        else:
            prev_flag = bool(previous)

        # 복원 방식 미지정 방어
        if not snapshot_id and not target_time and not prev_flag:
            return {
                "content": [{
                    "type": "text",
                    "text": ("❌ 복원 방식을 지정해야 합니다.\\n\\n"
                             "- `snapshot_id`: 특정 스냅샷으로 복원\\n"
                             "- `target_time`: 지정 시각 이전 상태로 복원\\n"
                             "- `previous`: 직전 스냅샷으로 복원\\n\\n"
                             "💡 먼저 `deepseek_view_timeline` 으로 이력을 확인하십시오.")
                }],
                "isError": True
            }

        # 진입 즉시 프로젝트 자동 전환
        active = self.bind_project_for(file_path)

        # target_time 문자열 → datetime 변환 (snapshot_engine.parse_when 위임)
        parsed_time = None
        if target_time:
            try:
                from core.snapshot_engine import parse_when as _parse_when
                parsed_time = _parse_when(target_time)
            except Exception as e:
                self.log(f"[MCP] target_time 파싱 실패: {e}")
                parsed_time = None

            if parsed_time is None:
                return {
                    "content": [{
                        "type": "text",
                        "text": (f"❌ target_time 해석 실패: `{target_time}`\\n\\n"
                                 "ISO 8601 문자열(예: 2026-10-23T23:48:29) 또는 "
                                 "epoch 초 단위 숫자를 사용하십시오.")
                    }],
                    "isError": True
                }

        try:
            res = self.pm.rollback_file(
                file_path,
                snapshot_id=snapshot_id or None,
                target_time=parsed_time,
                previous=prev_flag,
            )
        except Exception as e:
            self.log(f"[MCP] 롤백 예외: {e}")
            return {
                "content": [{"type": "text",
                             "text": f"❌ 롤백 실행 중 예외 발생: {e}\\n\\n{traceback.format_exc()}"}],
                "isError": True
            }

        if not res.get("success"):
            return {
                "content": [{
                    "type": "text",
                    "text": (f"❌ 롤백 실패\\n\\n"
                             f"- 대상: `{res.get('path') or file_path}`\\n"
                             f"- 사유: {res.get('message', '알 수 없는 오류')}\\n"
                             f"- 활성 프로젝트: `{active}`")
                }],
                "isError": True
            }

        stats = res.get("stats", {}) or {}
        lines = [
            "## ⏪ [스냅샷 롤백 완료]",
            f"**🎯 활성 프로젝트**: `{active}`",
            f"**📂 대상 파일**: `{res.get('path')}`",
            f"**🕰️ 복원 스냅샷**: `{res.get('snapshot_id') or '-'}`",
            f"**🔐 롤백 직전 보존 스냅샷**: `{res.get('backup_path') or '-'}`",
            f"**📊 변경량**: +{stats.get('added', 0)} / -{stats.get('removed', 0)}",
            ""
        ]

        diff_text = (res.get("diff") or "").strip()
        if diff_text:
            diff_lines = diff_text.splitlines()
            if len(diff_lines) > 60:
                diff_lines = diff_lines[:60] + ["... (이하 생략)"]
            lines.append("### 🔀 롤백 diff (롤백 전 → 롤백 후)")
            lines.append("```diff")
            lines.extend(diff_lines)
            lines.append("```")
        else:
            lines.append("_내용 변경이 없었습니다 (이미 해당 상태와 동일)._")

        lines.append("")
        lines.append("💡 롤백 직전 상태도 스냅샷으로 보존되었으므로, "
                     "필요 시 `deepseek_rollback_file` 로 되돌릴 수 있습니다.")

        return {"content": [{"type": "text", "text": "\\n".join(lines)}]}

    # ------------------------------------------------------------------
    # JSON-RPC stdio 루프
    # ------------------------------------------------------------------
    def run_stdio(self):''',
)

# 2-4) _tool_autonomous_codegen 배너 — 스냅샷 시각 + diff 정보 보강
mcp_patch(
    "mcp-codegen-snapshot-banner",
    '''        header = f"**🎯 활성 프로젝트**: `{active}`\\n**♻️ 핫리로드**: {reload_status}\\n\\n"
        return {"content": [{"type": "text", "text": header + (output or "") + force_sync_banner + verify_banner + syntax_info}]}''',
    '''        # ------------------------------------------------------------------
        # [SNAPSHOT-ENGINE] 시간별 스냅샷 생성 시각 + diff 정보 배너
        #   - 저장 직후 원장에서 최신 스냅샷을 조회하여 생성 시각/ID/변경량을 표시
        #   - 조회 실패는 결과 보고를 막지 않는다 (예외 격리)
        # ------------------------------------------------------------------
        snapshot_banner = ""
        if full_p:
            try:
                tl = self.pm.get_timeline(full_p, limit=1)
                if tl.get("success") and tl.get("timeline"):
                    rec = tl["timeline"][0]
                    sid = rec.get("snapshot_id", "")
                    created = rec.get("created_at", "")
                    note = rec.get("note", "") or "-"
                    size = rec.get("size", 0)

                    diff_line = "- **변경량**: 변경 없음 (최초 스냅샷 또는 동일 내용)"
                    try:
                        d = self.pm.get_diff(sid, full_p)
                        if d.get("success") and d.get("changed"):
                            diff_line = (
                                "- **변경량**: +%d / -%d (직전 스냅샷 `%s` 대비)"
                                % (d.get("added_lines", 0), d.get("removed_lines", 0),
                                   d.get("from_snapshot_id", "") or "-")
                            )
                    except Exception as e:
                        self.log(f"[MCP] 스냅샷 diff 조회 실패: {e}")

                    snapshot_banner = (
                        "\\n\\n### 🕰️ [시간별 스냅샷 기록]\\n"
                        "- **스냅샷 ID**: `%s`\\n"
                        "- **생성 시각**: %s\\n"
                        "- **메모**: %s\\n"
                        "- **크기**: %sB\\n"
                        "%s\\n"
                        "- **타임라인 전체**: %s건 (원장: timeline.jsonl)"
                        % (sid, created, note, size, diff_line, tl.get("total", 0))
                    )
                else:
                    snapshot_banner = (
                        "\\n\\n### 🕰️ [시간별 스냅샷 기록]\\n"
                        "⚪ 스냅샷 엔진이 비활성 상태이거나 기록된 이력이 없습니다."
                    )
            except Exception as e:
                self.log(f"[MCP] 스냅샷 배너 생성 실패: {e}")
                snapshot_banner = (
                    "\\n\\n### 🕰️ [시간별 스냅샷 기록]\\n"
                    "⚠️ 스냅샷 정보 조회 실패: %s" % e
                )

        header = f"**🎯 활성 프로젝트**: `{active}`\\n**♻️ 핫리로드**: {reload_status}\\n\\n"
        return {"content": [{"type": "text", "text": header + (output or "") + force_sync_banner + verify_banner + snapshot_banner + syntax_info}]}''',
)


# ======================================================================
# 패치 적용 엔진
# ======================================================================
def apply_patches(path, patches, label):
    with io.open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    before_lines = text.count("\n") + 1
    applied = 0
    ok = True

    for name, search, replace in patches:
        count = text.count(search)
        if count == 0:
            print("[SKIP] %s — SEARCH 미발견" % name)
            ok = False
            continue
        if count > 1:
            print("[ABORT] %s — SEARCH %d회 중복 (모호한 치환)" % (name, count))
            ok = False
            continue
        text = text.replace(search, replace, 1)
        applied += 1
        print("[OK] %s" % name)

    if not ok:
        print("%s: 미적용 패치 존재 — 파일 기록 중단 (applied=%d/%d)"
              % (label, applied, len(patches)))
        return False

    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)

    after_lines = text.count("\n") + 1
    print("%s: PATCH_OK applied=%d/%d, LINES %d -> %d"
          % (label, applied, len(patches), before_lines, after_lines))
    return True


def main():
    ok_pm = apply_patches(PM, PM_PATCHES, "project_manager.py")
    ok_mcp = apply_patches(MCP, MCP_PATCHES, "mcp_server.py")
    return 0 if (ok_pm and ok_mcp) else 1


if __name__ == "__main__":
    sys.exit(main())
