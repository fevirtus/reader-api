"""Preset catalog for the pinned VieNeu 3.7.1 model; IDs stay stable across releases."""

import hashlib
from pathlib import Path

MODEL_VERSION = "vieneu-3.7.1-61b85e3-normalizer1-aac64"
PREVIEW_TEXT = (
    "Buổi sớm, ánh nắng khẽ len qua khung cửa. "
    "Cô mở cuốn sách, mỉm cười và bắt đầu một hành trình mới."
)
PREVIEW_REVISION = hashlib.sha256((MODEL_VERSION + PREVIEW_TEXT).encode()).hexdigest()[:20]
_PRESETS = [
    ("anh-khoi", "Anh Khôi", "Nam", "Bắc"),
    ("minh-duc", "Minh Đức", "Nam", "Bắc"),
    ("pham-tuyen", "Phạm Tuyên", "Nam", "Bắc"),
    ("thai-son", "Thái Sơn", "Nam", "Nam"),
    ("xuan-vinh", "Xuân Vĩnh", "Nam", "Bắc"),
    ("thanh-binh", "Thanh Bình", "Nam", "Bắc"),
    ("truc-ly", "Trúc Ly", "Nữ", "Bắc"),
    ("ngoc-linh", "Ngọc Linh", "Nữ", "Bắc"),
    ("doan-trang", "Đoan Trang", "Nữ", "Bắc"),
    ("mai-anh", "Mai Anh", "Nữ", "Bắc"),
    ("thuc-doan", "Thục Đoan", "Nữ", "Nam"),
    ("minh-triet", "Minh Triết", "Nam", "Nam"),
    ("thuy-dung", "Thùy Dung", "Nữ", "Nam"),
    ("quang-son", "Quang Sơn", "Nam", "Trung"),
    ("ngoc-tran", "Ngọc Trân", "Nữ", "Trung"),
    ("my-duyen", "Mỹ Duyên", "Nữ", "Nam"),
    ("quynh-anh", "Quỳnh Anh", "Nữ", "Bắc"),
    ("duc-tri", "Đức Trí", "Nam", "Nam"),
    ("kim-thanh", "Kim Thanh", "Nữ", "Nam"),
    ("ngoc-huyen", "Ngọc Huyền", "Nữ", "Bắc"),
    ("adam", "Adam", "Nam", "Nam"),
    ("manh-dung", "Mạnh Dũng", "Nam", "Bắc"),
    ("minh-quan", "Minh Quân", "Nam", "Bắc"),
]
VOICES = [
    {
        "id": id_,
        "name": name,
        "modelVoice": name,
        "gender": gender,
        "region": region,
        "default": id_ == "anh-khoi",
    }
    for id_, name, gender, region in _PRESETS
]


def preview_directory(root: Path) -> Path:
    return root / "audiobook-previews" / PREVIEW_REVISION
