from __future__ import annotations

from importlib.resources import files


def build_channel() -> str:
    try:
        return files("electridrive").joinpath("build_channel.txt").read_text(
            encoding="utf-8"
        ).strip()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return ""


def window_title() -> str:
    channel = build_channel()
    if channel == "CI-QA":
        return "ElectriDrive — CI-QA — NOT FOR CUSTOMERS"
    if channel == "CUSTOMER-CANDIDATE":
        return "ElectriDrive — CUSTOMER CANDIDATE — NOT RELEASED"
    return "ElectriDrive"
