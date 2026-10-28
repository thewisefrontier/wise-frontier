# -*- coding: utf-8 -*-
"""[기업 분석] 저장 없이 생성만 시험(2026-10-28). one_off_task.yml(script=one_off/company_analysis_dryrun.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import company_analysis_writer  # noqa: E402

company_analysis_writer.run(dry_run=True)
