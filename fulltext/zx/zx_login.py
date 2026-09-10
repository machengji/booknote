# -*- coding: utf-8 -*-
import os as _os, sys as _sys
_here = _os.path.dirname(_os.path.abspath(__file__))
_ft = _here if _os.path.isfile(_os.path.join(_here, 'envutil.py')) else _os.path.dirname(_here)
if _ft not in _sys.path:
    _sys.path.insert(0, _ft)
from envutil import DATA, PDFS, LOGS, FT, zju_creds  # noqa: E402
# 知献图书馆登录（Stage B）：用绑定会话提交验证码+账号
import requests, pickle, re, sys, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
BASE = "http://lib.zxsju.com"
KEY = sys.argv[1]
USER, PWD = _os.environ.get("ZX_USER") or "", _os.environ.get("ZX_PWD") or ""

with open("_zx_session.pkl", "rb") as f:
    s = pickle.load(f)
s.headers["User-Agent"] = UA
s.headers["Referer"] = BASE + "/e/member/login/"
r = s.post(BASE + "/e/member/doaction.php",
           data={"enews": "login", "ecmsfrom": "", "tobind": "0", "username": USER,
                 "password": PWD, "lifetime": "315360000", "key": KEY, "Submit": " 登 录 "},
           timeout=25, allow_redirects=True)
body = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', r.text))
print("LOGIN st", r.status_code, "final", r.url, "cookies", list(s.cookies.keys()))
print("MSG:", body[:300])
if "验证码" in body or "密码" in body or "不存在" in body:
    print("LOGIN FAILED")
else:
    with open("_zx_login.pkl", "wb") as f:
        pickle.dump(s, f)
    print("LOGIN SESSION SAVED")