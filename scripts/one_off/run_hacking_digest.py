# -*- coding: utf-8 -*-
"""오늘의 해킹 첫 회 발행(2026-11-26, 1회용 — 이후는 digest.yml이 매일 실행). 실행: one_off_task.yml(script=one_off/run_hacking_digest.py)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ai_digest  # noqa: E402

ai_digest.run("hacking")
