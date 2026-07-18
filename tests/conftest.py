"""pytest 共通設定: リポジトリルートを import パスに通す。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
