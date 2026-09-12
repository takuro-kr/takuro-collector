from takuro_collector.sites.kinoshita import KinoshitaAdapter
from takuro_collector.verified_photos import VERIFIED_PHOTOS


def test_16061_verified_photos_keep_one_exterior_then_plan_then_rooms():
    sources = [{"url": "https://kinoshita-chintai.com/img.php?img="+name, "sha256": row["sha256"]} for name,row in VERIFIED_PHOTOS.items() if "014304" in name]
    ordered = KinoshitaAdapter._order_photo_sources(sources)
    kinds = [row["kind"] for row in ordered]
    assert len(ordered) == 18
    assert kinds[:2] == ["exterior", "floorplan"]
    assert kinds[2:7] == ["interior_room"] * 5
    assert kinds[-1] == "exterior"
    assert kinds.count("floorplan") == 1
    assert KinoshitaAdapter._kin_photo_kind({"url":"https://kinoshita-chintai.com/img.php?img=813013_014304_0002_06.jpg"}) == "interior_toilet"


def test_unknown_numeric_slot_is_not_a_floorplan_or_room():
    for code in (6,9,12,23):
        assert KinoshitaAdapter._kin_photo_kind({"url":f"https://kinoshita-chintai.com/img.php?img=999_888_0001_{code:02}.jpg"}) == "interior_other"


def test_16998_content_hashes_put_rooms_before_kitchen():
    sources = [{"url":"https://kinoshita-chintai.com/img.php?img="+name,"sha256":row["sha256"]} for name,row in VERIFIED_PHOTOS.items() if "011542" in name]
    ordered = KinoshitaAdapter._order_photo_sources(sources)
    kinds = [row["kind"] for row in ordered]
    assert kinds[:2] == ["exterior", "floorplan"]
    assert kinds.index("interior_room") < kinds.index("interior_kitchen")
    assert kinds.count("interior_room") >= 8

def test_numeric_suffix_never_claims_a_room_category():
    for code in (1, 2, 7, 13, 23, 41, 42):
        assert KinoshitaAdapter._kin_photo_kind({"url":f"https://kinoshita-chintai.com/img.php?img=999_888_0001_{code:02}.jpg"}) == "interior_other"
