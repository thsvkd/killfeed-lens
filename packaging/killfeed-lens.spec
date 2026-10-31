# PyInstaller spec: dist/killfeed-lens/ (onedir: kfocr.exe, console). The Electron app
# (app/package.mjs) ships this onedir as its engine and runs `kfocr.exe engine`; the old Tk GUI is
# still `kfocr.exe gui --live`, with no exe of its own. No portable onefile for now.
# Build: uv run pyinstaller --noconfirm --distpath dist --workpath build packaging/killfeed-lens.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
metadata = []
for package in ("rapidocr-onnxruntime", "dxcam", "httpx", "PyYAML"):
    metadata.extend(copy_metadata(package, recursive=True))

a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    # OCR config + ONNX models and kfocr's bundled icon and glyph templates are package data;
    # screen.py reads dxcam's dist-info version. Distribution metadata also carries the
    # dependency licenses/third-party notices (notably ONNX Runtime and OpenCV).
    datas=collect_data_files("rapidocr_onnxruntime") + collect_data_files("kfocr")
    + collect_data_files("onnxruntime", includes=["LICENSE", "ThirdPartyNotices.txt"])
    + list(dict.fromkeys(metadata))
    + [(str(root / "packaging" / "licenses"), "third-party-licenses")],
    hiddenimports=collect_submodules("dxcam"),  # dxcam imports its processors via import_module
)
pyz = PYZ(a.pure)
icon = str(root / "packaging" / "killfeed-lens.ico")

cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="kfocr", console=True, upx=False,
          icon=icon)
COLLECT(cli, a.binaries, a.datas, name="killfeed-lens", upx=False)
