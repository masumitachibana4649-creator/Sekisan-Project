from dataclasses import dataclass
from decimal import Decimal
import json
import os
import re
import tempfile
from pathlib import Path

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
    wall_surfaces: dict | None = None


@dataclass(frozen=True)
class PdfAnalysisResult:
    rooms: list[AnalyzedRoom]
    memo: str


PAGE_LABELS = {
    "page_1f_plan": "1F平面図",
    "page_2f_plan": "2F平面図",
    "page_3f_plan": "3F平面図",
    "page_development_start": "展開図開始頁",
    "page_development_end": "展開図終了頁",
    "page_1f_ceiling_plan": "1F天井伏図",
    "page_2f_ceiling_plan": "2F天井伏図",
    "page_3f_ceiling_plan": "3F天井伏図",
}

PLAN_PAGE_KEYS = ("page_1f_plan", "page_2f_plan", "page_3f_plan")
NON_WALLPAPER_ROOM_LABELS = ("浴室", "バルコニー")
ROOM_LABEL_PATTERNS = {
    "洋室": ("洋室",),
    "収納": ("収納",),
    "廊下": ("廊下",),
    "玄関": ("玄関",),
    "トイレ": ("トイレ",),
    "LDK": ("LDK", "ＬＤＫ"),
    "洗面所": ("洗面所",),
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

    validation_warnings = _validate_room_extraction(pdf_path, parsed_pages, rooms)

    page_summary = "、".join(
        f"{PAGE_LABELS[key]}={value}P" for key, value in parsed_pages.items() if value is not None
    )
    warnings = " ".join(ai_result["warnings"] + validation_warnings)
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
    if parsed.get("page_development_start") is None and parsed.get("page_development_end") is not None:
        raise ValueError("展開図開始頁が指定されていません。")
    if parsed.get("page_development_start") is not None and parsed.get("page_development_end") is None:
        raise ValueError("展開図終了頁が指定されていません。")
    if (
        parsed.get("page_development_start") is not None
        and parsed.get("page_development_end") is not None
        and parsed["page_development_start"] > parsed["page_development_end"]
    ):
        raise ValueError("展開図開始頁は展開図終了頁以下にしてください。")
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
    selected_pdf_path = None
    try:
        selected_pdf_path = _write_selected_pages_pdf(pdf_path, parsed_pages)
        with open(selected_pdf_path, "rb") as pdf_file:
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
        if selected_pdf_path is not None:
            Path(selected_pdf_path).unlink(missing_ok=True)

    return _parse_ai_analysis_response(_response_text(response))


def _write_selected_pages_pdf(pdf_path, parsed_pages):
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise ValueError("PDF図面の読取ライブラリがインストールされていません。") from exc

    selected_pages = _analysis_page_numbers(parsed_pages)
    if not selected_pages:
        raise ValueError("AI読取対象の図面ページが指定されていません。")

    try:
        reader = PdfReader(str(pdf_path))
        writer = PdfWriter()
        for page_number in selected_pages:
            writer.add_page(reader.pages[page_number - 1])
        temp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        try:
            writer.write(temp_file)
        finally:
            temp_file.close()
        return temp_file.name
    except Exception as exc:
        raise ValueError("AI読取用の指定ページPDFを作成できませんでした。") from exc


def _analysis_page_numbers(parsed_pages):
    selected_pages = []
    development_start = parsed_pages.get("page_development_start")
    development_end = parsed_pages.get("page_development_end")
    for key, page_number in parsed_pages.items():
        if key in {"page_development_start", "page_development_end"}:
            continue
        if page_number is not None and page_number not in selected_pages:
            selected_pages.append(page_number)
    if development_start is not None and development_end is not None:
        for page_number in range(development_start, development_end + 1):
            if page_number not in selected_pages:
                selected_pages.append(page_number)
    return selected_pages


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
- 1面〜4面ごとの壁面積・開口部面積 wall_surfaces

ルール:
- 回答内の文章は、warnings と evidence を含めて必ず日本語で書いてください。
- 単位はすべてメートルまたは平方メートルに変換してください。
- mm表記はmに換算してください。
- C.H、CH、天井高が読める場合は height_m に反映してください。
- 天井高が部屋ごとに読めない場合は、図面内の標準天井高を使ってください。
- 壁紙対象外と判断できる浴室、バルコニー、屋外部分は除外してください。
- 平面図上に見える室名は必ず一度すべて洗い出し、浴室・バルコニー・屋外部分以外は原則としてroomsに含めてください。
- 同じ室名が複数ある場合は統合せず、位置や階で区別してください。例: 洋室が3つ見える場合は3行、収納が複数見える場合は「収納 一式」または個別行として漏れなく含めてください。
- 廊下、階段、玄関、トイレ、洗面所、収納、物入もクロス施工対象として含めてください。
- 収納・物入・SIC・CL・パントリー・廊下・ホール・階段は、個別寸法が読めない場合でも「一式」や展開図の複合部屋名として必ずroomsに含めてください。
- 周長が直接読めないが部屋寸法や面積表から合理的に算出できる場合は算出してください。
- 開口部は外部開口と内部開口を分けて検討してください。
- wall_surfaces は face_1, face_2, face_3, face_4 の4面を必ず返してください。
- 展開図が「1面」「2面」「3面」「4面」の表記なら、そのまま face_1〜face_4 に対応させてください。
- 展開図が「A」「B」「C」「D」の表記なら、A=face_1、B=face_2、C=face_3、D=face_4 と読み替えてください。
- wall_surfaces の width_m は展開図に書かれたその面の壁幅、surface_area_m2 は壁幅×天井高で計算した開口部を差し引く前の壁面積、opening_area_m2 はその面の開口部面積にしてください。
- 例: 展開図に「1,592.5」、天井高が2.4mの場合、width_m=1.5925、surface_area_m2=3.82 としてください。surface_area_m2 に天井高 2.4 をそのまま入れないでください。
- 展開図の面番号と部屋名の対応が不確かな場合は、根拠を evidence に書いて confidence を下げてください。
- どうしても方向別の割り当てが不確かな場合は、合計値を均等配分せず、読めた壁面に配分して confidence を下げてください。
- 外部開口は展開図の窓・玄関ドア・サッシを、展開図の縮尺と既知寸法から幅・高さを推定して部屋へ割り当ててください。
- 2階バルコニーに面したサッシなど高さが読み取りにくい開口は、同種の1階サッシ高さを保守的に流用して推定してください。
- 内部開口は平面図の室内扉・収納扉を、平面図の縮尺と既知寸法から幅を推定し、高さが読めない場合は標準建具高さ2.0mで控えめに推定してください。
- 天井面積は平面図または天井伏図から読み取れる寸法・面積を優先してください。
- 寸法明記のない開口は過大控除を避け、控えめな値にしてください。推定開口を使った場合は evidence に必ず「推定開口」「展開図/平面図」「推定した幅・高さ」「外部開口/内部開口の内訳」を書いてください。
- 開口部が図面上で確認できない場合だけ opening_area_m2 を 0 にし、warnings に理由を入れてください。
- 不確かな値は evidence に根拠と推定理由を書き、confidence を下げてください。
- ロール本数、ロス率込み面積、金額は計算しないでください。アプリ側で計算します。
""".strip()


def _analysis_schema():
    surface_schema = {
        "type": "object",
        "properties": {
            "width_m": {"type": "number", "minimum": 0},
            "surface_area_m2": {"type": "number", "minimum": 0},
            "opening_area_m2": {"type": "number", "minimum": 0},
        },
        "required": ["width_m", "surface_area_m2", "opening_area_m2"],
        "additionalProperties": False,
    }
    wall_surfaces_schema = {
        "type": "object",
        "properties": {
            "face_1": surface_schema,
            "face_2": surface_schema,
            "face_3": surface_schema,
            "face_4": surface_schema,
        },
        "required": ["face_1", "face_2", "face_3", "face_4"],
        "additionalProperties": False,
    }
    room_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "perimeter_m": {"type": "number", "minimum": 0},
            "height_m": {"type": "number", "minimum": 0},
            "opening_area_m2": {"type": "number", "minimum": 0},
            "ceiling_area_m2": {"type": "number", "minimum": 0},
            "wall_surfaces": wall_surfaces_schema,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "string"},
        },
        "required": [
            "name",
            "perimeter_m",
            "height_m",
            "opening_area_m2",
            "ceiling_area_m2",
            "wall_surfaces",
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
            note_parts.append(f"根拠: {evidence}")
        note_parts.append(f"AI信頼度: {confidence}")
        wall_surfaces = _wall_surfaces_from_ai(room.get("wall_surfaces"))
        rooms.append(
            AnalyzedRoom(
                name=name,
                perimeter_m=_decimal_from_ai(room.get("perimeter_m"), "0"),
                height_m=_decimal_from_ai(room.get("height_m"), "0"),
                opening_area_m2=_decimal_from_ai(room.get("opening_area_m2"), "0"),
                ceiling_area_m2=_decimal_from_ai(room.get("ceiling_area_m2"), "0"),
                note=" / ".join(note_parts),
                wall_surfaces=wall_surfaces,
            )
        )

    warnings = [str(warning).strip() for warning in payload.get("warnings", []) if str(warning).strip()]
    return {"rooms": rooms, "warnings": warnings}


def _wall_surfaces_from_ai(value):
    if not isinstance(value, dict):
        return None

    surfaces = {}
    surface_keys = (("east", "face_1"), ("west", "face_2"), ("south", "face_3"), ("north", "face_4"))
    for field, ai_key in surface_keys:
        surface = value.get(ai_key)
        if surface is None:
            surface = value.get(field)
        if not isinstance(surface, dict):
            return None
        surfaces[field] = {
            "width_m": _decimal_from_ai(surface.get("width_m"), "0"),
            "surface_area_m2": _decimal_from_ai(surface.get("surface_area_m2"), "0"),
            "opening_area_m2": _decimal_from_ai(surface.get("opening_area_m2"), "0"),
        }
    return surfaces


def _validate_room_extraction(pdf_path, parsed_pages, rooms):
    plan_text = _plan_page_text(pdf_path, parsed_pages)
    if not plan_text:
        return []

    expected_counts = _expected_room_counts(plan_text)
    if not expected_counts:
        return []

    actual_room_text = " ".join(room.name for room in rooms)
    actual_counts = {
        label: _actual_room_count(actual_room_text, aliases, expected_counts[label])
        for label, aliases in ROOM_LABEL_PATTERNS.items()
        if label in expected_counts
    }
    missing = {
        label: count - actual_counts.get(label, 0)
        for label, count in expected_counts.items()
        if count > actual_counts.get(label, 0)
    }
    if not missing:
        return []

    expected_total = sum(expected_counts.values())
    actual_total = len(rooms)
    missing_total = sum(missing.values())
    missing_summary = "、".join(f"{label}{count}件" for label, count in missing.items())
    if _missing_only_secondary_spaces(missing):
        return [f"平面図上の補助空間候補に対し、未抽出の可能性があります: {missing_summary}。"]

    if expected_total >= 5 and (actual_total < (expected_total * Decimal("0.60")) or missing_total >= 3):
        raise ValueError(
            "PDF AI読取の部屋抽出数が不足している可能性があります。"
            f"平面図上の室名候補は約{expected_total}件、抽出結果は{actual_total}件です。"
            f"未抽出候補: {missing_summary}。"
        )

    return [f"平面図上の室名候補に対し、未抽出の可能性があります: {missing_summary}。"]


def _missing_only_secondary_spaces(missing):
    return bool(missing) and set(missing).issubset({"収納", "廊下", "玄関"})


def _plan_page_text(pdf_path, parsed_pages):
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        texts = []
        for key in PLAN_PAGE_KEYS:
            page_number = parsed_pages.get(key)
            if not page_number:
                continue
            texts.append(reader.pages[page_number - 1].extract_text() or "")
        return "\n".join(texts)
    except Exception:
        return ""


def _expected_room_counts(plan_text):
    text = _normalize_text(plan_text)
    counts = {}
    for label, aliases in ROOM_LABEL_PATTERNS.items():
        count = sum(_count_label_occurrences(text, alias) for alias in {_normalize_text(alias) for alias in aliases})
        if count:
            counts[label] = count
    for excluded in NON_WALLPAPER_ROOM_LABELS:
        counts.pop(excluded, None)
    return counts


def _actual_room_count(room_text, aliases, expected_count):
    text = _normalize_text(room_text)
    if "一式" in str(room_text) and any(alias in text for alias in {_normalize_text(alias) for alias in aliases}):
        return expected_count
    return sum(1 for alias in {_normalize_text(alias) for alias in aliases} for match in re.finditer(re.escape(alias), text))


def _count_label_occurrences(text, label):
    return len(re.findall(re.escape(_normalize_text(label)), text))


def _normalize_text(value):
    return str(value).replace("ＬＤＫ", "LDK").upper()


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
        "page_development_start": 8,
        "page_development_end": 8,
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
        development_page = parsed_pages.get("page_development_start")
        hallway_note = f"{page}P/{development_page}P: 図面寸法から概算" if development_page else f"{page}P: 図面寸法から概算"
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
