# -*- coding: utf-8 -*-
"""Remote test: SHU WoS only, one day, log why hop stuck."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wos_pipeline as wos

wos.WOS_ENTRIES = [(5954, "WOS(shu)")]


def main():
    wos.log("=== wos shu test 2026-09-01 ===")
    rc = wos.run_days(["2026-09-01"], wait_login=False, auto_login=False, headed=True)
    wos.log("SHU_TEST", "ok" if rc == 0 else "rc=%d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
