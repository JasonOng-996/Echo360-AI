# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

_datas=[]; _bins=[]; _hidden=[]
for pkg in ["playwright", "imageio_ffmpeg", "openai", "anthropic", "httpx", "pydantic"]:
    try:
        d,b,h=collect_all(pkg); _datas += d; _bins += b; _hidden += h
    except Exception:
        _hidden += collect_submodules(pkg)
_datas += [("LICENSE.txt", "."), ("PRIVACY.md", "."), ("config.default.json", "."), ("prompts/lecture_summary.txt", "prompts"), ("prompts/video_analysis.txt", "prompts")]

a=Analysis(["app_gui.py"], pathex=[], binaries=_bins, datas=_datas, hiddenimports=_hidden, hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name="EchoLectureAI",debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,disable_windowed_traceback=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,upx_exclude=[],name="EchoLectureAI")
