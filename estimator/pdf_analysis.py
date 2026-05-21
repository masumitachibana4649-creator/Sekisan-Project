from dataclasses import dataclass
from decimal import Decimal
import json
import os

try:
    from django.conf import settings
except ImportError:  # pragma: no cover - allows isolated script usage.
    settings = None


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

    ai_result = _extract_rooms_with_ai(pdf_path, parsed_pages)
    rooms = ai_result["rooms"]
    if not rooms:
        raise ValueError("PDFから計算対象の部屋を抽出できませんでした。")

    page_summary = "、".join(
        f"{PAGE_LABELS[key]}={value}P" for key, value in parsed_pages.items() if value is not None
    )
    warnings = " ".join(ai_result["warnings"])
    warning_text = f" 注意: {warnings}" if warnings else ""
    return PdfAnalysisResult(
        rooms=rooms,
        memo=(
            f"PDF AI読取: {page_summary}。"
            "部屋名・周長・天井高・開口部面積・天井面積をAIで抽出し、"
            f"壁紙量とロール本数はシステムの計算式で算出。{warning_text}"
        ),
    )


def _pdf_page_count(pdf_path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF図面の読取ライブラリがインストールされていません。") from exc

    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise ValueError("PDFファイルを解析できませんでした。")
    return len(reader.pages)


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


def _extract_rooms_with_ai(pdf_path, parsed_pages):
    api_key = _setting("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY が設定されていないためPDF AI読取を実行できません。")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ValueError("openai パッケージがインストールされていません。") from exc

    model = _setting("OPENAI_PDF_ANALYSIS_MODEL", "gpt-4o")
    client = OpenAI(api_key=api_key)
    uploaded_file = None
    try:
        with open(pdf_path, "rb") as pdf_file:
            uploaded_file = client.files.create(file=pdf_file, purpose="user_data")

        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded_file.id},
                        {"type": "input_text", "text": _analysis_prompt(parsed_pages)},
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "wallpaper_pdf_analysis",
                    "strict": True,
                    "schema": _analysis_schema(),
                }
            },
        )
    except Exception as exc:
        raise ValueError(f"PDF AI読取に失敗しました: {exc}") from exc
    finally:
        if uploaded_file is not None:
            try:
                client.files.delete(uploaded_file.id)
            except Exception:
                pass

    return _parse_ai_analysis_response(_response_text(response))


def _analysis_prompt(parsed_pages):
    page_lines = "\n".join(
        f"- {PAGE_LABELS[key]}: {value}ページ" for key, value in parsed_pages.items() if value is not None
    )
    return f"""
添付PDFは壁紙積算用の建築図面です。以下の指定ページだけを主な根拠にして、クロス施工対象の部屋を抽出してください。

{page_lines}

抽出対象:
- 部屋名
- 部屋の周長 perimeter_m
- 天井高 height_m
- 窓・ドアなどの開口部面積 opening_area_m2
- 天井面積 ceiling_area_m2

ルール:
- 単位はすべてメートルまたは平方メートルに変換してください。
- mm表記はmに換算してください。
- C.H、CH、天井高が読める場合は height_m に反映してください。
- 天井高が部屋ごとに読めない場合は、図面内の標準天井高を使ってください。
- 壁紙対象外と判断できる浴室、バルコニー、屋外部分は除外してください。
- 周長が直接読めないが部屋寸法や面積表から合理的に算出できる場合は算出してください。
- 開口部寸法が読み取れない場合は opening_area_m2 を 0 にし、warnings に理由を入れてください。
- 不確かな値は evidence に根拠と推定理由を書き、confidence を下げてください。
- ロール本数、ロス率込み面積、金額は計算しないでください。アプリ側で計算します。
""".strip()


def _analysis_schema():
    room_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "perimeter_m": {"type": "number", "minimum": 0},
            "height_m": {"type": "number", "minimum": 0},
            "opening_area_m2": {"type": "number", "minimum": 0},
            "ceiling_area_m2": {"type": "number", "minimum": 0},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "string"},
        },
        "required": [
            "name",
            "perimeter_m",
            "height_m",
            "opening_area_m2",
            "ceiling_area_m2",
            "confidence",
            "evidence",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "rooms": {"type": "array", "items": room_schema},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["rooms", "warnings"],
        "additionalProperties": False,
    }


def _response_text(response):
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    texts = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                texts.append(text)
    if texts:
        return "\n".join(texts)
    raise ValueError("AI応答からJSON本文を取得できませんでした。")


def _parse_ai_analysis_response(response_text):
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError("AI応答がJSONとして解析できませんでした。") from exc

    rooms = []
    for index, room in enumerate(payload.get("rooms", []), start=1):
        name = str(room.get("name") or f"部屋{index}").strip()
        confidence = _decimal_from_ai(room.get("confidence"), "0")
        evidence = str(room.get("evidence") or "").strip()
        note_parts = []
        if evidence:
            note_parts.append(evidence)
        note_parts.append(f"AI信頼度 {confidence}")
        rooms.append(
            AnalyzedRoom(
                name=name,
                perimeter_m=_decimal_from_ai(room.get("perimeter_m"), "0"),
                height_m=_decimal_from_ai(room.get("height_m"), "0"),
                opening_area_m2=_decimal_from_ai(room.get("opening_area_m2"), "0"),
                ceiling_area_m2=_decimal_from_ai(room.get("ceiling_area_m2"), "0"),
                note=" / ".join(note_parts)[:160],
            )
        )

    warnings = [str(warning).strip() for warning in payload.get("warnings", []) if str(warning).strip()]
    return {"rooms": rooms, "warnings": warnings}


def _decimal_from_ai(value, default):
    try:
        return Decimal(str(value if value is not None else default)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal(default)


def _setting(name, default=None):
    if settings is not None and getattr(settings, "configured", False):
        value = getattr(settings, name, None)
        if value is not None:
            return value
    return os.environ.get(name, default)


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
