"""
Structured briefs cho design agents — buộc khách điền chi tiết để AI output đúng.
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


# ─── ARCHITECTURE BRIEF ─────────────────────────────────────────
class LandSpec(BaseModel):
    width_m: float = Field(gt=0, le=200)
    depth_m: float = Field(gt=0, le=500)
    shape: Literal["vuong_dai","vuong","tam_giac","method","khac"] = "vuong_dai"
    setback_front_m: float = Field(default=4.0, ge=0, le=30)
    setback_back_m: float = Field(default=2.0, ge=0, le=30)
    setback_side_m: float = Field(default=2.0, ge=0, le=30)


class FamilySpec(BaseModel):
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    elderly: int = Field(default=0, ge=0, le=20)
    helpers: int = Field(default=0, ge=0, le=10)


class RoomRequirement(BaseModel):
    """1 phòng cụ thể. Buộc nhập area + must_have để AI render chi tiết đúng."""
    type: str = Field(min_length=2, max_length=64,
                       description="phong_khach | phong_an | phong_bep | master_bedroom | …")
    floor: int = Field(default=1, ge=1, le=20)
    area_m2: float = Field(gt=0, le=500)
    ceiling_h_m: float = Field(default=3.2, ge=2.4, le=8.0)
    must_have: list[str] = Field(default_factory=list,
                                  description="Items bắt buộc: bar_counter, walk_in_closet, fireplace…")
    nice_to_have: list[str] = Field(default_factory=list)
    style_notes: str | None = Field(default=None, max_length=500)
    privacy_level: Literal["public","semi_private","private"] = "public"


class PhongThuySpec(BaseModel):
    chu_nhan_menh: Literal["kim","moc","thuy","hoa","tho"] | None = None
    nam_sinh: int | None = Field(default=None, ge=1900, le=2100)
    huong_chinh_yeu_cau: Literal["bac","dong_bac","dong","dong_nam","nam","tay_nam","tay","tay_bac"] | None = None
    avoid_directions: list[str] = Field(default_factory=list)


class StructuredArchitectureBrief(BaseModel):
    project_name: str = Field(min_length=3, max_length=128)
    project_type: Literal["biet_thu","nha_pho","can_ho","resort","office","retail","mixed_use"] = "biet_thu"
    location_city: str = Field(default="TP.HCM", max_length=64)
    location_district: str | None = Field(default=None, max_length=64)
    climate_zone: Literal["bac","trung","nam"] = "nam"

    land: LandSpec
    orientation: Literal["bac","dong_bac","dong","dong_nam","nam","tay_nam","tay","tay_bac"] = "nam"
    floors: int = Field(default=2, ge=1, le=20)
    family: FamilySpec
    rooms: list[RoomRequirement] = Field(min_length=1, max_length=60)

    style: Literal["tropical_modern","indochine","japandi","minimalist","traditional_vietnamese","contemporary","mediterranean","custom"] = "tropical_modern"
    custom_style_notes: str | None = Field(default=None, max_length=2000)

    materials_preferred: list[str] = Field(default_factory=list,
        description="go_pomu, da_binh_dinh, kinh_low_e, da_marble, gach_terrazzo…")
    materials_avoid: list[str] = Field(default_factory=list)
    color_palette: list[str] = Field(default_factory=list, max_length=8,
        description="Hex codes: ['#F5F1EA','#3D3A35']")

    budget_vnd: int | None = Field(default=None, ge=100_000_000, le=1_000_000_000_000)
    timeline_months: int | None = Field(default=None, ge=1, le=60)
    phong_thuy: PhongThuySpec | None = None

    sustainability_priorities: list[str] = Field(default_factory=list,
        description="passive_cooling, solar_panels, rainwater_harvest, green_roof…")

    references_image_urls: list[str] = Field(default_factory=list, max_length=10)
    must_avoid: list[str] = Field(default_factory=list, max_length=20)
    additional_notes: str | None = Field(default=None, max_length=2000)


# ─── INTERIOR BRIEF (single space) ──────────────────────────────
class StructuredInteriorBrief(BaseModel):
    project_name: str = Field(min_length=3, max_length=128)
    space_type: Literal["phong_khach","phong_ngu","phong_an","phong_bep","phong_lam_viec","ban_cong","san_thuong","spa","cafe","showroom","khac"] = "phong_khach"
    area_m2: float = Field(gt=0, le=500)
    ceiling_h_m: float = Field(default=3.0, ge=2.4, le=8.0)
    dimensions_l_w: tuple[float, float] | None = None

    natural_light_direction: Literal["bac","dong","tay","nam","khong_co"] = "dong"
    main_window_size: str | None = Field(default=None, max_length=64,
        description="VD: 3m x 2.4m FTC")

    style: Literal["tropical_modern","indochine","japandi","minimalist","traditional_vietnamese","scandinavian","industrial","luxury","custom"] = "tropical_modern"
    custom_style_notes: str | None = Field(default=None, max_length=1500)

    must_have_items: list[str] = Field(default_factory=list,
        description="VD: ['bar_counter_3m','tv_unit_3.5m','reading_corner','walk_in_closet']")
    avoid_items: list[str] = Field(default_factory=list)

    materials_preferred: list[str] = Field(default_factory=list)
    materials_avoid: list[str] = Field(default_factory=list)
    color_palette: list[str] = Field(default_factory=list, max_length=8)

    occupants_age_range: Literal["young","middle","elderly","mixed"] = "mixed"
    pets: list[str] = Field(default_factory=list, description="dog, cat, ...")
    daily_use_priorities: list[str] = Field(default_factory=list,
        description="lam_viec, doc_sach, tiep_khach, nau_an, …")

    budget_vnd: int | None = Field(default=None, ge=10_000_000, le=100_000_000_000)
    references_image_urls: list[str] = Field(default_factory=list, max_length=10)

    additional_notes: str | None = Field(default=None, max_length=2000)


# ─── PRODUCT / FASHION simpler structured ───────────────────────
class StructuredProductBrief(BaseModel):
    project_name: str = Field(min_length=3, max_length=128)
    product_category: Literal["consumer_electronics","packaging","household","wearable","furniture","tool","khac"] = "consumer_electronics"
    target_market: Literal["vn_only","asean","global"] = "vn_only"
    target_price_vnd: int | None = Field(default=None, ge=10_000)

    form_factor_dimensions: str | None = Field(default=None, max_length=128,
        description="LxWxH cm, weight g")
    primary_function: str = Field(min_length=5, max_length=500)
    secondary_functions: list[str] = Field(default_factory=list, max_length=10)
    user_persona: str | None = Field(default=None, max_length=500)

    materials_preferred: list[str] = Field(default_factory=list,
        description="anodized_aluminum, recycled_plastic, bamboo, steel_brushed…")
    color_options: list[str] = Field(default_factory=list, max_length=8)
    style_inspiration: list[str] = Field(default_factory=list,
        description="apple, dieter_rams, muji, jasper_morrison…")

    sustainability_priorities: list[str] = Field(default_factory=list)
    competitor_products: list[str] = Field(default_factory=list, max_length=5)
    references_image_urls: list[str] = Field(default_factory=list, max_length=10)
    additional_notes: str | None = Field(default=None, max_length=2000)


class StructuredFashionBrief(BaseModel):
    project_name: str = Field(min_length=3, max_length=128)
    collection_type: Literal["rtw","casual","workwear","streetwear","evening","sustainable","traditional"] = "rtw"
    season: Literal["ss","fw","cruise","resort","year_round"] = "year_round"
    target_year: int = Field(default=2026, ge=2024, le=2030)

    target_market: Literal["vn","asean","japan","korea","global"] = "vn"
    target_age: Literal["gen_z","millennial","gen_x","mixed"] = "gen_z"
    target_gender: Literal["female","male","unisex"] = "female"

    silhouette_keywords: list[str] = Field(default_factory=list,
        description="oversized, fitted, A_line, drape, structured…")
    fabric_preferences: list[str] = Field(default_factory=list,
        description="lua_ha_dong, lanen_bao_loc, cotton_organic, wool_blend…")
    color_palette: list[str] = Field(default_factory=list, max_length=10)

    must_haves: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    style_inspiration: list[str] = Field(default_factory=list,
        description="jacquemus, the_row, dries_van_noten, vietnam_local…")

    target_price_vnd_per_item: int | None = Field(default=None, ge=100_000)
    target_quantity_per_item: int | None = Field(default=None, ge=1)

    references_image_urls: list[str] = Field(default_factory=list, max_length=10)
    additional_notes: str | None = Field(default=None, max_length=2000)


# ─── STRUCTURAL ENGINEER BRIEF ──────────────────────────────────
class StructuredStructuralBrief(BaseModel):
    project_name: str = Field(min_length=3, max_length=128)
    project_type: Literal["biet_thu","nha_pho","can_ho","cao_oc","cong_nghiep","cau"] = "biet_thu"
    floors_above: int = Field(default=2, ge=1, le=80)
    floors_below: int = Field(default=0, ge=0, le=5)
    typical_floor_area_m2: float = Field(gt=0, le=10000)
    typical_floor_height_m: float = Field(default=3.5, ge=2.4, le=10)

    location_city: str = Field(default="TP.HCM", max_length=64)
    soil_assumed_bearing_kpa: int | None = Field(default=None, ge=50, le=2000,
        description="Sức chịu đất giả định, kPa")
    seismic_zone: Literal["1","2","3","4","unknown"] = "unknown"
    wind_zone: Literal["I","II","III","IV","V","unknown"] = "II"

    structural_system_preferred: Literal["bt_cot_thep","khung_thep","hon_hop","go","auto"] = "auto"
    span_max_m: float = Field(default=7.0, ge=2, le=30)

    live_load_kn_m2: float = Field(default=2.0, ge=0.5, le=20,
        description="Tải sống điển hình, theo TCVN 2737")
    dead_load_extra_kn_m2: float = Field(default=2.0, ge=0, le=20)

    must_consider: list[str] = Field(default_factory=list,
        description="open_floor_plan, large_overhang, swimming_pool_top, …")
    code_compliance: list[str] = Field(default=["TCVN_5574","TCVN_2737","TCVN_5573"])

    additional_notes: str | None = Field(default=None, max_length=2000)
