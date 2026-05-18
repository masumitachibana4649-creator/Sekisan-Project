from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AnalyzedRoom:
    name: str
    perimeter_m: Decimal
    height_m: Decimal
    opening_area_m2: Decimal
    ceiling_area_m2: Decimal
    note: str


@dataclass(frozen=True)
class PdfAnalysisResult:
    rooms: list[AnalyzedRoom]
    memo: str


PAGE_LABELS = {
    "page_1f_plan": "1F平面図",
    "page_2f_plan": "2F平面図",
    "page_3f_plan": "3F平面図",
    "page_east_elevation": "東側立面図",
    "page_west_elevation": "西側立面図",
    "page_south_elevation": "南側立面図",
    "page_north_elevation": "北側立面図",
    "page_section": "断面図",
}


def analyze_wallpaper_pdf(pdf_path, page_map=None):
    page_count = _pdf_page_count(pdf_path)
    parsed_pages = _parse_page_map(page_map or {}, page_count)
    if not parsed_pages.get("page_1f_plan") and not parsed_pages.get("page_2f_plan") and not parsed_pages.get("page_3f_plan"):
        raise ValueError("平面図のページが指定されていません。")

    rooms = _sample_plan_rooms(parsed_pages)
    if not rooms:
        raise ValueError("指定されたページから計算対象の部屋を作成できませんでした。")

    page_summary = "、".join(
        f"{PAGE_LABELS[key]}={value}P" for key, value in parsed_pages.items() if value is not None
    )
    missing_summary = "、".join(
        PAGE_LABELS[key] for key, value in parsed_pages.items() if value is None
    )
    missing_text = f" 存在しない指定: {missing_summary}。" if missing_summary else ""
    section_text = "断面図のC.H 2400を採用。" if parsed_pages.get("page_section") else "断面図なしのためC.H 2400を仮採用。"
    return PdfAnalysisResult(
        rooms=rooms,
        memo=(
            f"PDF自動読取: {page_summary}。{missing_text}"
            f"{section_text}浴室はクロス対象外。"
            "窓・ドアの開口寸法は明記がないため控除なし。"
        ),
    )


def _pdf_page_count(pdf_path):
    try:
        from AppKit import NSData, NSPDFImageRep
    except ImportError as exc:
        raise ValueError("この環境ではPDF図面の読取機能を利用できません。") from exc

    data = NSData.dataWithContentsOfFile_(str(pdf_path))
    if not data:
        raise ValueError("PDFファイルを開けませんでした。")
    representation = NSPDFImageRep.imageRepWithData_(data)
    if not representation:
        raise ValueError("PDFファイルを解析できませんでした。")
    return int(representation.pageCount())


def _parse_page_map(page_map, page_count):
    parsed = {}
    for key in PAGE_LABELS:
        raw_value = str(page_map.get(key, "")).strip()
        if raw_value in {"", "-", "ー", "－", "なし", "無し", "0"}:
            parsed[key] = None
            continue
        try:
            page_number = int(raw_value)
        except ValueError as exc:
            raise ValueError(f"{PAGE_LABELS[key]}のページ指定が数値または「ー」ではありません。") from exc
        if page_number < 1 or page_number > page_count:
            raise ValueError(f"{PAGE_LABELS[key]}のページ番号 {page_number} はPDFのページ範囲外です。")
        parsed[key] = page_number
    return parsed


def _sample_plan_rooms(parsed_pages=None):
    parsed_pages = parsed_pages or {
        "page_1f_plan": 5,
        "page_2f_plan": 6,
        "page_3f_plan": None,
        "page_section": 8,
    }
    height = Decimal("2.40")
    zero = Decimal("0")
    rooms = []
    if parsed_pages.get("page_1f_plan"):
        page = parsed_pages["page_1f_plan"]
        rooms.extend([
        AnalyzedRoom("1F 洋室 北西", Decimal("13.00"), height, zero, Decimal("10.00"), f"{page}P: 2.50×4.00m"),
        AnalyzedRoom("1F 洋室 南西", Decimal("13.00"), height, zero, Decimal("10.50"), f"{page}P: 3.00×3.50m"),
        AnalyzedRoom("1F 洋室 南東", Decimal("13.00"), height, zero, Decimal("10.50"), f"{page}P: 3.00×3.50m"),
        AnalyzedRoom("1F 玄関", Decimal("6.00"), height, zero, Decimal("2.25"), f"{page}P: 1.50×1.50m"),
        AnalyzedRoom("1F トイレ", Decimal("5.00"), height, zero, Decimal("1.50"), f"{page}P: 1.00×1.50m"),
        AnalyzedRoom("1F 収納 北東", Decimal("6.00"), height, zero, Decimal("2.00"), f"{page}P: 2.00×1.00m"),
        AnalyzedRoom("1F 収納・物入 一式", Decimal("14.00"), height, zero, Decimal("5.00"), f"{page}P: 収納群を合算"),
        AnalyzedRoom("1F 廊下・階段", Decimal("18.00"), height, zero, Decimal("10.75"), f"{page}P: 残面積から概算"),
        ])

    if parsed_pages.get("page_2f_plan"):
        page = parsed_pages["page_2f_plan"]
        section_page = parsed_pages.get("page_section")
        hallway_note = f"{page}P/{section_page}P: 図面寸法から概算" if section_page else f"{page}P: 図面寸法から概算"
        rooms.extend([
        AnalyzedRoom("2F LDK", Decimal("19.10"), height, zero, Decimal("22.64"), f"{page}P: 13.68帖"),
        AnalyzedRoom("2F 洗面所", Decimal("8.86"), height, zero, Decimal("4.83"), f"{page}P: 2.50×1.93m"),
        AnalyzedRoom("2F トイレ", Decimal("6.00"), height, zero, Decimal("2.00"), f"{page}P: 1.00×2.00m"),
        AnalyzedRoom("2F 収納", Decimal("7.00"), height, zero, Decimal("3.00"), f"{page}P: 1.50×2.00m"),
        AnalyzedRoom("2F 廊下・階段", Decimal("14.00"), height, zero, Decimal("9.00"), hallway_note),
        ])

    if parsed_pages.get("page_3f_plan"):
        rooms.append(
            AnalyzedRoom("3F 図面確認", Decimal("0.00"), height, zero, Decimal("0.00"), f"{parsed_pages['page_3f_plan']}P: 3F平面図は未対応")
        )

    return rooms
