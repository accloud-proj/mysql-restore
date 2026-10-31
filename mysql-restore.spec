from pathlib import Path


root = Path(SPECPATH)
analysis = Analysis(
    [str(root / "scripts" / "entrypoint.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[(str(root / "build" / "version.txt"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="mysql-restore",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
