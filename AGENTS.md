# 🏛️ Antigravity 6-Member Joint Council Workspace Architecture

This project is governed by the **6-Member Joint Council (Gemini Debater + DeepSeek 5-Agent Council)** architecture.

## Role Separation Contract

1. **Gemini (Debater & Advisor)**:
   - Evaluates context, communicates with user, proposes ideas, and debates solutions.
   - **Strict Restriction**: No direct write permissions to source code. Never bypasses the DeepSeek Council for code edits.

2. **DeepSeek Council (5-Agent Core)**:
   - **DeepSeek-R1 (Lead Architect)**: Root cause analysis, system architecture design, and debater feedback evaluation.
   - **DeepSeek-V3 (Auditor)**: Critical review, anti-tampering check, 24/7 autonomous safety verification.
   - **DeepSeek-V3 (Senior Coder)**: Exclusive implementation authority for production source code.
   - **QA Sandbox Verifier**: `py_compile` pre-flight syntax checks and unit verification.
   - **Git Deployment Guardian**: Safe diff presentation and user-approval gate. No unauthorized commits or pushes.

All modifications must adhere to the modular clean design principle and end-to-end autonomous pipeline standards.
