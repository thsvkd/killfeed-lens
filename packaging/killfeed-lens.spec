# PyInstaller spec: dist/killfeed-lens/ (onedir: KillfeedLens.exe windowed + kfocr.exe console)
# and dist/killfeed-lens-<version>-portable.exe (windowed onefile).
# Build: uv run pyinstaller --noconfirm --distpath dist --workpath build packaging/killfeed-lens.spec
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    # OCR config + ONNX models and kfocr's bundled icon templates are package data;
    # screen.py reads dxcam's dist-info version.
    datas=collect_data_files("rapidocr_onnxruntime") + collect_data_files("kfocr")
    + copy_metadata("dxcam"),
    hiddenimports=collect_submodules("dxcam"),  # dxcam imports its processors via import_module
)
pyz = PYZ(a.pure)

gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="KillfeedLens", console=False, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="kfocr", console=True, upx=False)
COLLECT(gui, cli, a.binaries, a.datas, name="killfeed-lens", upx=False)

EXE(pyz, a.scripts, a.binaries, a.datas, [], name=f"killfeed-lens-{version}-portable",
    console=False, upx=False)
