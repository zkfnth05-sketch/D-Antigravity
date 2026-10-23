# -*- coding: utf-8 -*-
import os
import time
import asyncio
import psutil
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable

class WatchdogSentinel:
    def __init__(self, project_manager, interval: float = 2.0):
        self.pm = project_manager
        self.interval = interval
        self.running = False
        self.subscribers = []
        self.last_log_positions = {}
        self.last_alert = None
        self.active_processes = []

    def subscribe(self, callback: Callable):
        self.subscribers.append(callback)

    def unsubscribe(self, callback: Callable):
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        for sub in list(self.subscribers):
            try:
                await sub({"type": event_type, "data": data, "timestamp": datetime.now().strftime("%H:%M:%S")})
            except Exception:
                pass

    def get_system_health(self) -> Dict[str, Any]:
        cpu_percent = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        return {
            "cpu_percent": cpu_percent,
            "memory_percent": mem.percent,
            "memory_used_gb": round(mem.used / (1024**3), 2),
            "memory_total_gb": round(mem.total / (1024**3), 2)
        }

    def scan_bot_processes(self) -> List[Dict[str, Any]]:
        bots = []
        keywords = ["run_stock", "aura", "kmarket", "daemon", "shorts", "insurance", "kakao", "naver"]
        for p in psutil.process_iter(["pid", "name", "cmdline", "create_time", "cpu_percent", "memory_info"]):
            try:
                cmdline = " ".join(p.info["cmdline"] or [])
                if "python" in p.info["name"].lower() or "cmd" in p.info["name"].lower():
                    matched_kws = [kw for kw in keywords if kw.lower() in cmdline.lower()]
                    if matched_kws:
                        bots.append({
                            "pid": p.info["pid"],
                            "name": os.path.basename(p.info["cmdline"][-1]) if p.info["cmdline"] else p.info["name"],
                            "cmdline": cmdline[:100],
                            "tag": matched_kws[0],
                            "status": "RUNNING",
                            "create_time": datetime.fromtimestamp(p.info["create_time"]).strftime("%H:%M:%S")
                        })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.active_processes = bots
        return bots

    async def check_log_anomalies(self):
        project_path = self.pm.active_project
        if not os.path.exists(project_path):
            return

        error_keywords = ["error", "exception", "traceback", "failed", "timeout", "락 획득 타임아웃", "충돌"]
        
        # Scan recent .log files
        for root, _, files in os.walk(project_path):
            for file in files:
                if file.endswith((".log", ".txt", ".json")) and ("log" in file.lower() or "history" in file.lower()):
                    full_path = os.path.join(root, file)
                    try:
                        file_size = os.path.getsize(full_path)
                        last_pos = self.last_log_positions.get(full_path, max(0, file_size - 4096))
                        
                        if file_size > last_pos:
                            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                f.seek(last_pos)
                                new_text = f.read()
                                self.last_log_positions[full_path] = f.tell()

                                # Check for error lines
                                for line in new_text.splitlines():
                                    line_clean = line.strip()
                                    if line_clean and any(kw in line_clean.lower() for kw in error_keywords):
                                        alert = {
                                            "file": os.path.basename(full_path),
                                            "message": line_clean[:200],
                                            "level": "ERROR",
                                            "path": full_path
                                        }
                                        self.last_alert = alert
                                        await self.broadcast("watchdog_alert", alert)
                                        break
                    except Exception:
                        pass

    async def run_loop(self):
        self.running = True
        while self.running:
            try:
                health = self.get_system_health()
                bots = self.scan_bot_processes()
                await self.check_log_anomalies()
                
                await self.broadcast("watchdog_status", {
                    "health": health,
                    "bots_count": len(bots),
                    "bots": bots,
                    "active_project": os.path.basename(self.pm.active_project),
                    "status": "HEALTHY" if not self.last_alert else "ALERT"
                })
            except Exception as e:
                print(f"Watchdog error: {e}")
            
            await asyncio.sleep(self.interval)

    def stop(self):
        self.running = False
