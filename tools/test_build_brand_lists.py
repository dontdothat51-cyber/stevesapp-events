# -*- coding: utf-8 -*-
"""Offline check of the brand table in tools/build_brand_lists.py (no TMDB calls).
  python tools/test_build_brand_lists.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_brand_lists as bl


def test_brands_table():
    B = bl.BRANDS
    assert len(B) == 13, len(B)
    ids = [b["id"] for b in B]
    assert len(set(ids)) == len(ids), ids
    assert len({b["door"] for b in B}) == len(B)
    for b in B:
        assert b["id"] and b["id"] == b["id"].lower() and b["door"] == b["door"].upper() and b["name"], b
        assert b["providers"] and all(isinstance(p, int) and not isinstance(p, bool) and p > 0 for p in b["providers"]), b
        assert all(k in bl.KINDS for k in b.get("kinds", ["movie", "tv"])), b
    by = {b["id"]: b for b in B}
    # 10/30 additions: the app's tokens HULU / STARZ / AMCPLUS; plain US subscription ids, movies AND series, flatrate
    for i, door, pid in (("hulu", "HULU", 15), ("starz", "STARZ", 43), ("amcplus", "AMCPLUS", 526)):
        b = by[i]
        assert b["door"] == door and b["providers"] == [pid] and b.get("kinds", ["movie", "tv"]) == ["movie", "tv"], b
        assert not b.get("rotate") and not b.get("and_company") and b["companies"] == [], b
        assert bl.base_params(b, "movie")["with_watch_monetization_types"] == "flatrate"


if __name__ == "__main__":
    test_brands_table()
    print("brand table OK:", len(bl.BRANDS), "brands")
