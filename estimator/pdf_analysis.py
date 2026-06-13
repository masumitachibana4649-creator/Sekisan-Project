"""PDF図面から壁紙積算に必要な部屋情報を抽出する処理を定義する。"""

from dataclasses import dataclass
from decimal import Decimal
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

try:
    from django.conf import settings
except ImportError:  # pragma: no cover - allows isolated script usage.
    settings = None


@dataclass(frozen=True)
class AnalyzedRoom:
    """PDF解析で抽出した部屋情報を表すデータクラス。

    Attributes:
        name: 名前。
        perimeter_m: 部屋の周長。
        height_m: 天井高。
        opening_area_m2: 開口部面積。
        ceiling_area_m2: 天井面積。
        note: 備考。
        wall_surfaces: 1面から4面までの壁面情報。
    """
    name: str
    perimeter_m: Decimal
    height_m: Decimal
    opening_area_m2: Decimal
    ceiling_area_m2: Decimal
    note: str
    wall_surfaces: dict | None = None


@dataclass(frozen=True)
class PdfAnalysisResult:
    """PDF解析結果全体を表すデータクラス。

    Attributes:
        rooms: AI解析で抽出した部屋一覧。
        memo: 解析結果の補足メモ。
        missing_rooms: 面積情報を抽出できなかった部屋名一覧。
        room_candidates: 表ページなどから検出した部屋候補一覧。
    """
    rooms: list[AnalyzedRoom]
    memo: str
    missing_rooms: list[str] | None = None
    room_candidates: list["RoomCandidate"] | None = None


@dataclass(frozen=True)
class RoomCandidate:
    """表ページなどから検出した部屋候補を表すデータクラス。

    Attributes:
        floor: 階数ラベル。
        name: 名前。
        area_m2: 部屋候補の面積。
        source: 階数やページ番号の判定に使う文字列。
        page: 候補を検出したページ番号。
    """
    floor: str
    name: str
    area_m2: Decimal | None
    source: str
    page: int


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
CEILING_PLAN_PAGE_KEYS = ("page_1f_ceiling_plan", "page_2f_ceiling_plan", "page_3f_ceiling_plan")
ROOM_CANDIDATE_PAGE_KEYS = PLAN_PAGE_KEYS + CEILING_PLAN_PAGE_KEYS
TABLE_PAGE_KEYWORDS = (
    ("居室区画面積表", "居室区画面積表"),
    ("床面積表", "床面積表"),
    ("室内仕上表", "室内仕上表"),
    ("内部仕上表", "内部仕上表"),
    ("建具表", "建具表"),
)
NON_WALLPAPER_ROOM_LABELS = ("浴室", "バルコニー")
NON_WALLPAPER_CANDIDATE_NAMES = ("UB", "浴室", "バルコニー", "ポーチ", "屋外")
ROOM_LABEL_PATTERNS = {
    "和室": ("和室",),
    "洋室": ("洋室", "子供室", "子供部屋", "主寝室", "寝室"),
    "収納": ("収納", "物入", "押入", "納戸", "小屋裏収納", "CL", "ＣＬ", "SIC", "ＳＩＣ", "パントリー"),
    "廊下": ("廊下", "ホール"),
    "玄関": ("玄関",),
    "トイレ": ("トイレ", "便所", "WC", "ＷＣ"),
    "LDK": ("LDK", "ＬＤＫ"),
    "台所": ("台所", "キッチン"),
    "食堂": ("食堂", "ダイニング"),
    "洗面所": ("洗面所", "洗面", "洗面脱衣", "脱衣", "ランドリー"),
}

ANALYSIS_PROMPT_INTRO = "添付PDFは壁紙積算用の建築図面です。以下の指定ページだけを主な根拠にして、クロス施工対象の部屋を抽出してください。"

ANALYSIS_PROMPT_TARGETS = """抽出対象:
- 部屋名
- 部屋の周長 perimeter_m
- 天井高 height_m
- 窓・ドアなどの開口部面積 opening_area_m2
- 天井面積 ceiling_area_m2
- 1面〜4面ごとの壁面積・開口部面積 wall_surfaces"""

ANALYSIS_PROMPT_RULES = """ルール:

基本:
- 回答内の文章は、warnings と evidence を含めて必ず日本語で書いてください。
- 単位はすべてメートルまたは平方メートルに変換し、mm表記はmに換算してください。
- C.H、CH、天井高が読める場合は height_m に反映してください。部屋ごとに読めない場合は図面内の標準天井高を使ってください。
- 壁紙対象外と判断できる浴室、UB、バルコニー、ポーチ、屋外部分は除外してください。

部屋候補の優先順位と網羅:
- 表ページから検出した部屋候補がある場合は、その候補リストを部屋一覧の主根拠として全件確認してください。表ページ候補を優先し、平面図と天井伏図の室名候補も補助根拠として漏れ確認に使ってください。
- 表ページ候補のうち対象室は、面積や開口部が推定でも rooms または missing_rooms のどちらかに必ず含めてください。面積がある場合はその部屋の ceiling_area_m2 として原則そのまま採用し、平面図や展開図から推定した別値で上書きしないでください。
- 平面図上に見える室名は必ず一度すべて洗い出し、対象室は原則としてroomsに含めてください。展開図が読み取りやすい和室A/Bなど一部の部屋だけで回答を終えず、指定された平面図ページ全体を対象にして、各階の居室・水回り・収納・廊下・玄関を最後に再確認してください。
- LDK、洋室、主寝室、トイレ、洗面所、脱衣、ランドリー、和室、台所、食堂、便所、物入、押入、納戸、子供室、寝室、廊下、階段、玄関、収納は通常のクロス施工対象として扱ってください。
- 同じ室名が複数ある場合は統合せず、位置や階で区別してください。階が異なる同名部屋は別部屋として扱い、例: 1F トイレ と 2F トイレ は必ず別々に確認してください。
- 平面図で別室名・別区画として確認できる居間、台所・食堂、LDK、寝室、洋室、和室、畳室は、近接していても統合せず別部屋として扱ってください。展開図だけで同種の室名が複数見える場合は、平面図上の室数を超えて増やさないでください。
- 収納、物入、SIC、CL、パントリー、PS、吹抜、ファミリークローゼット、廊下、ホール、階段は、個別寸法が読めない場合でも「一式」や展開図の複合部屋名としてroomsに含めてください。複数の収納を一式扱いにする場合は、同じ階の収納候補をすべてカバーしていることを evidence に明記してください。CL、PS、吹抜、ファミリークローゼットは収納一式に含めず、個別候補として扱ってください。
- roomsに入れるだけの面積・開口部情報をどうしても抽出できない室名候補は、missing_rooms に「階 部屋名」の形式で入れてください。missing_roomsにはroomsに入れた部屋名を重複して入れず、クロス施工対象だが面積入力が必要な部屋だけにしてください。

寸法・面積の推定:
- 天井面積は平面図または天井伏図から読み取れる寸法・面積を優先してください。
- 周長が直接読めないが部屋寸法や面積表から合理的に算出できる場合は算出してください。
- 展開図に4面すべての情報がない部屋でも、平面図または天井伏図で確認できる部屋は省略しないでください。読めた寸法、畳数、天井面積、近い類似部屋から周長・壁幅・開口部を合理的に推定してroomsに含めてください。
- 部屋ごとの展開図が見つからない場合は、wall_surfaces を空にせず、平面図の長方形寸法または周長から face_1〜face_4 の width_m と surface_area_m2 を推定してください。その場合は evidence に「展開図未確認のため平面図から推定」と明記し、confidence を下げてください。

wall_surfaces:
- wall_surfaces は face_1, face_2, face_3, face_4 の4面を必ず返してください。
- 展開図が「1面」「2面」「3面」「4面」の表記なら、そのまま face_1〜face_4 に対応させてください。展開図が「A」「B」「C」「D」の表記なら、A=face_1、B=face_2、C=face_3、D=face_4 と読み替えてください。
- wall_surfaces の width_m は展開図に書かれたその面の壁幅、surface_area_m2 は壁幅×天井高で計算した開口部を差し引く前の壁面積、opening_area_m2 はその面の開口部面積にしてください。
- 例: 展開図に「1,592.5」、天井高が2.4mの場合、width_m=1.5925、surface_area_m2=3.82 としてください。surface_area_m2 に天井高 2.4 をそのまま入れないでください。
- 展開図の面番号と部屋名の対応が不確かな場合は、根拠を evidence に書いて confidence を下げてください。どうしても方向別の割り当てが不確かな場合は、合計値を均等配分せず、読めた壁面に配分してください。

開口部:
- 開口部は外部開口と内部開口を分けて検討してください。
- 外部開口は展開図の窓・玄関ドア・サッシを、展開図の縮尺と既知寸法から幅・高さを推定して部屋へ割り当ててください。2階バルコニーに面したサッシなど高さが読み取りにくい開口は、同種の1階サッシ高さを保守的に流用して推定してください。
- 内部開口は平面図の室内扉・収納扉を、平面図の縮尺と既知寸法から幅を推定し、高さが読めない場合は標準建具高さ2.0mで控えめに推定してください。
- 寸法明記のない開口は過大控除を避け、控えめな値にしてください。推定開口を使った場合は evidence に必ず「推定開口」「展開図/平面図」「推定した幅・高さ」「外部開口/内部開口の内訳」を書いてください。
- 開口部が図面上で確認できない場合だけ opening_area_m2 を 0 にし、warnings に理由を入れてください。

出力:
- 不確かな値は evidence に根拠と推定理由を書き、confidence を下げてください。
- ロール本数、ロス率込み面積、金額は計算しないでください。アプリ側で計算します。"""

ANALYSIS_PROMPT_FINAL_CHECK = """最終チェック:
- 表ページ候補のうち対象室が rooms または missing_rooms に全件含まれているか確認してください。
- 平面図・天井伏図の主要室が漏れていないか確認してください。
- 階違い同名部屋を統合していないか確認してください。
- 全室の wall_surfaces が face_1〜face_4 を持っているか確認してください。
- 表ページ候補の面積がある部屋は ceiling_area_m2 に採用されているか確認してください。"""


def analyze_wallpaper_pdf(pdf_path, page_map=None, table_pages=None, allow_visual_table_detection=True):
    """PDF図面を解析して壁紙積算用の部屋情報を抽出する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        page_map: 画面で指定されたページ情報。
        table_pages: 表ページのラベルとページ番号。
        allow_visual_table_detection: 画像解析による表ページ検出を許可する場合はTrue。

    Returns:
        PDF解析結果。
    """
    page_count = _pdf_page_count(pdf_path)
    parsed_pages = _parse_page_map(page_map or {}, page_count)
    if not parsed_pages.get("page_1f_plan") and not parsed_pages.get("page_2f_plan") and not parsed_pages.get("page_3f_plan"):
        raise ValueError("平面図のページが指定されていません。")

    room_candidate_text = _room_candidate_page_text(pdf_path, parsed_pages)
    table_pages = (
        _deduplicate_table_pages(table_pages)
        if table_pages is not None
        else _detect_table_pages(pdf_path, allow_visual_detection=allow_visual_table_detection)
    )
    room_candidates = _room_table_candidates(pdf_path, table_pages)
    expected_counts = _expected_room_counts(room_candidate_text) if room_candidate_text else {}

    ai_result = _extract_rooms_with_ai(
        pdf_path,
        parsed_pages,
        expected_counts=expected_counts,
        table_pages=table_pages,
        room_candidates=room_candidates,
    )
    rooms = ai_result["rooms"]
    if not rooms:
        raise ValueError("PDFから計算対象の部屋を抽出できませんでした。")

    missing_rooms = ai_result.get("missing_rooms", [])
    missing_room_count = (
        _candidate_missing_room_count(rooms, missing_rooms, room_candidates)
        if room_candidates
        else _missing_room_count(missing_rooms, rooms)
    )
    validation_warnings = _validate_room_extraction(
        pdf_path,
        parsed_pages,
        rooms,
        room_candidate_text=room_candidate_text,
        expected_counts=expected_counts,
        missing_room_count=missing_room_count,
        missing_rooms=missing_rooms,
        room_candidates=room_candidates,
    )

    page_parts = [
        f"{PAGE_LABELS[key]}={value}P" for key, value in parsed_pages.items() if value is not None
    ]
    page_parts.extend(f"{label}={page}P" for label, page in table_pages)
    page_summary = "、".join(page_parts)
    room_count_summary = (
        f"件数内訳: AI抽出={len(rooms)}件、抽出失敗追加={missing_room_count}件、"
        f"表示合計={len(rooms) + missing_room_count}件。"
    )
    warnings = " ".join(ai_result["warnings"] + validation_warnings)
    warning_text = f" 注意: {warnings}" if warnings else ""
    return PdfAnalysisResult(
        rooms=rooms,
        memo=(
            f"PDF AI読取: {page_summary}。"
            "部屋名・周長・天井高・開口部面積・天井面積をAIで抽出し、"
            f"壁紙量とロール本数はシステムの計算式で算出。{room_count_summary}{warning_text}"
        ),
        missing_rooms=missing_rooms,
        room_candidates=room_candidates,
    )


def _pdf_page_count(pdf_path):
    """PDFのページ数を取得する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。

    Returns:
        PDFの総ページ数。
    """
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
    """画面で指定されたページ情報を検証して正規化する。

    Args:
        page_map: 画面で指定されたページ情報。
        page_count: PDFの総ページ数。

    Returns:
        検証済みのページ指定辞書。
    """
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


def _extract_rooms_with_ai(pdf_path, parsed_pages, expected_counts=None, table_pages=None, room_candidates=None):
    """OpenAI APIを使ってPDFから部屋情報を抽出する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        parsed_pages: 検証済みのページ指定。
        expected_counts: ページテキストから推定した部屋種別ごとの件数。
        table_pages: 表ページのラベルとページ番号。
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        AI応答を解析した部屋情報辞書。
    """
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
        selected_pdf_path = _write_selected_pages_pdf(
            pdf_path,
            parsed_pages,
            additional_pages=[page for _label, page in table_pages or []],
        )
        with open(selected_pdf_path, "rb") as pdf_file:
            uploaded_file = client.files.create(file=pdf_file, purpose="user_data")

        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded_file.id},
                        {
                            "type": "input_text",
                            "text": _analysis_prompt(
                                parsed_pages,
                                expected_counts=expected_counts,
                                table_pages=table_pages,
                                room_candidates=room_candidates,
                            ),
                        },
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


def _write_selected_pages_pdf(pdf_path, parsed_pages, additional_pages=None):
    """AI解析に必要なページだけを抽出した一時PDFを作成する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        parsed_pages: 検証済みのページ指定。
        additional_pages: 追加でAI解析に含めるページ番号。

    Returns:
        抽出した一時PDFファイルのパス。
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise ValueError("PDF図面の読取ライブラリがインストールされていません。") from exc

    selected_pages = _analysis_page_numbers(parsed_pages, additional_pages=additional_pages)
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


def _analysis_page_numbers(parsed_pages, additional_pages=None):
    """AI解析対象に含めるページ番号を重複なしで返す。

    Args:
        parsed_pages: 検証済みのページ指定。
        additional_pages: 追加でAI解析に含めるページ番号。

    Returns:
        AI解析対象のページ番号一覧。
    """
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
    for page_number in additional_pages or []:
        if page_number and page_number not in selected_pages:
            selected_pages.append(page_number)
    return selected_pages


def _analysis_prompt(parsed_pages, expected_counts=None, table_pages=None, room_candidates=None):
    """PDF解析AIへ渡す指示文を組み立てる。

    Args:
        parsed_pages: 検証済みのページ指定。
        expected_counts: ページテキストから推定した部屋種別ごとの件数。
        table_pages: 表ページのラベルとページ番号。
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        AI解析用のプロンプト文字列。
    """
    page_lines = "\n".join(
        f"- {PAGE_LABELS[key]}: {value}ページ" for key, value in parsed_pages.items() if value is not None
    )
    table_page_lines = _table_page_prompt_lines(table_pages)
    room_candidate_lines = _room_candidate_prompt_lines(room_candidates)
    expected_room_lines = _expected_room_prompt_lines(expected_counts)
    return f"""
{ANALYSIS_PROMPT_INTRO}

{page_lines}

自動検出した表ページ:
{table_page_lines}

表ページから検出した部屋候補:
{room_candidate_lines}

平面図・天井伏図テキストから検出した補助室名候補:
{expected_room_lines}

{ANALYSIS_PROMPT_TARGETS}

{ANALYSIS_PROMPT_RULES}

{ANALYSIS_PROMPT_FINAL_CHECK}
""".strip()


def _expected_room_prompt_lines(expected_counts):
    """部屋種別ごとの推定件数をプロンプト用の行に変換する。

    Args:
        expected_counts: ページテキストから推定した部屋種別ごとの件数。

    Returns:
        処理結果。
    """
    if not expected_counts:
        return "- なし"
    return "\n".join(f"- {label}: 約{count}件" for label, count in expected_counts.items())


def _table_page_prompt_lines(table_pages):
    """表ページ情報をプロンプト用の行に変換する。

    Args:
        table_pages: 表ページのラベルとページ番号。

    Returns:
        処理結果。
    """
    if not table_pages:
        return "- なし"
    return "\n".join(f"- {label}: {page}ページ" for label, page in table_pages)


def _room_candidate_prompt_lines(room_candidates):
    """部屋候補をプロンプト用の行に変換する。

    Args:
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        処理結果。
    """
    if not room_candidates:
        return "- なし"
    lines = []
    for candidate in room_candidates:
        area = f"{candidate.area_m2}m2" if candidate.area_m2 is not None else "面積不明"
        floor = candidate.floor or "階不明"
        lines.append(f"- {floor} {candidate.name}: {area}（{candidate.source} {candidate.page}P）")
    return "\n".join(lines)


def _analysis_schema():
    """PDF解析AIのJSON応答スキーマを返す。

    Returns:
        AI解析応答のJSON Schema。
    """
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
            "missing_rooms": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["rooms", "missing_rooms", "warnings"],
        "additionalProperties": False,
    }


def _response_text(response):
    """OpenAI応答から解析対象のテキストを取り出す。

    Args:
        response: OpenAI APIの応答オブジェクト。

    Returns:
        AI応答から取り出したテキスト。
    """
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
    """AI応答JSONをアプリ内部の解析結果へ変換する。

    Args:
        response_text: AI応答のJSON文字列。

    Returns:
        部屋・抽出失敗部屋・警告を含む辞書。
    """
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

    missing_rooms = [str(room_name).strip() for room_name in payload.get("missing_rooms", []) if str(room_name).strip()]
    warnings = [str(warning).strip() for warning in payload.get("warnings", []) if str(warning).strip()]
    return {"rooms": rooms, "missing_rooms": missing_rooms, "warnings": warnings}


def _wall_surfaces_from_ai(value):
    """AI応答の面別情報をアプリ内部形式へ変換する。

    Args:
        value: 変換または正規化する値。

    Returns:
        面別情報の辞書。
    """
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


def _validate_room_extraction(
    pdf_path,
    parsed_pages,
    rooms,
    room_candidate_text=None,
    expected_counts=None,
    missing_room_count=0,
    missing_rooms=None,
    room_candidates=None,
):
    """抽出済み部屋と候補情報を照合して警告を返す。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        parsed_pages: 検証済みのページ指定。
        rooms: 抽出済みの部屋情報。
        room_candidate_text: 部屋候補抽出に使うページテキスト。
        expected_counts: ページテキストから推定した部屋種別ごとの件数。
        missing_room_count: 抽出失敗として追加する部屋数。
        missing_rooms: 抽出できなかった部屋名の一覧。
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        検証警告メッセージの一覧。
    """
    room_candidate_text = (
        room_candidate_text if room_candidate_text is not None else _room_candidate_page_text(pdf_path, parsed_pages)
    )
    room_candidates = room_candidates or []
    if room_candidates:
        return _validate_room_candidates(rooms, missing_rooms or [], room_candidates)
    if not room_candidate_text:
        return []

    expected_counts = expected_counts if expected_counts is not None else _expected_room_counts(room_candidate_text)
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
    display_total = actual_total + missing_room_count
    missing_total = sum(missing.values())
    missing_summary = "、".join(f"{label}{count}件" for label, count in missing.items())
    if _missing_only_secondary_spaces(missing):
        return [f"平面図上の補助空間候補に対し、未抽出の可能性があります: {missing_summary}。"]

    if expected_total >= 5 and (actual_total < (expected_total * Decimal("0.60")) or missing_total >= 3):
        return [
            "PDF AI読取の部屋抽出数が不足している可能性があります。"
            f"平面図・天井伏図上の室名候補は約{expected_total}件、"
            f"AI抽出は{actual_total}件、抽出失敗追加は{missing_room_count}件、"
            f"表示合計は{display_total}件です。"
            f"未抽出候補: {missing_summary}。"
            "積算結果を確認し、必要に応じて編集で部屋・面積を補正してください。"
        ]

    return [f"平面図・天井伏図上の室名候補に対し、未抽出の可能性があります: {missing_summary}。"]


def _validate_room_candidates(rooms, missing_rooms, room_candidates):
    """表ページ候補が表示対象に含まれているかを検証する。

    Args:
        rooms: 抽出済みの部屋情報。
        missing_rooms: 抽出できなかった部屋名の一覧。
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        表ページ候補に対する検証警告メッセージの一覧。
    """
    displayed = set()
    storage_bundle_floors = set()
    for room in rooms:
        displayed.update(_displayed_room_match_keys(room.name, room.note))
        if _is_storage_bundle(room.name):
            floor = _floor_label_from_text(f"{room.name} {room.note}")
            if floor:
                storage_bundle_floors.add(floor)
    for room_name in missing_rooms or []:
        displayed.update(_displayed_room_match_keys(room_name))
        if _is_storage_bundle(room_name):
            floor = _floor_label_from_text(room_name)
            if floor:
                storage_bundle_floors.add(floor)
    missing_candidates = [
        candidate
        for candidate in room_candidates
        if _normalize_room_name(candidate.name)
        and not _candidate_is_displayed(candidate, displayed, storage_bundle_floors)
    ]
    if not missing_candidates:
        return []

    expected_total = len(room_candidates)
    ai_total = len(rooms)
    missing_total = len(missing_candidates)
    display_total = ai_total + missing_total
    missing_summary = "、".join(_candidate_label(candidate) for candidate in missing_candidates[:12])
    if len(missing_candidates) > 12:
        missing_summary += f"、ほか{len(missing_candidates) - 12}件"
    return [
        "PDF AI読取の部屋抽出数が不足している可能性があります。"
        f"表ページ上の部屋候補は{expected_total}件、"
        f"AI抽出は{ai_total}件、抽出失敗追加は{missing_total}件、"
        f"表示合計は{display_total}件です。"
        f"未表示候補: {missing_summary}。"
        "積算結果を確認し、必要に応じて編集で部屋・面積を補正してください。"
    ]


def _candidate_label(candidate):
    """部屋候補の表示ラベルを返す。

    Args:
        candidate: 部屋候補。

    Returns:
        表示用の部屋候補名。
    """
    floor = f"{candidate.floor} " if candidate.floor else ""
    return f"{floor}{candidate.name}"


def _missing_only_secondary_spaces(missing):
    """不足候補が補助空間だけかどうかを返す。

    Args:
        missing: 不足している部屋種別ごとの件数。

    Returns:
        処理結果。
    """
    return bool(missing) and set(missing).issubset({"収納", "廊下", "玄関"})


def _plan_page_text(pdf_path, parsed_pages):
    """平面図ページのテキストを取得する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        parsed_pages: 検証済みのページ指定。

    Returns:
        処理結果。
    """
    return _page_text_for_keys(pdf_path, parsed_pages, PLAN_PAGE_KEYS)


def _room_candidate_page_text(pdf_path, parsed_pages):
    """部屋候補抽出に使うページテキストを取得する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        parsed_pages: 検証済みのページ指定。

    Returns:
        処理結果。
    """
    return _page_text_for_keys(pdf_path, parsed_pages, ROOM_CANDIDATE_PAGE_KEYS)


def _page_text_for_keys(pdf_path, parsed_pages, page_keys):
    """指定ページキーに対応するPDFテキストを取得する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        parsed_pages: 検証済みのページ指定。
        page_keys: 抽出対象のページキー。

    Returns:
        処理結果。
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        texts = []
        seen_pages = set()
        for key in page_keys:
            page_number = parsed_pages.get(key)
            if not page_number or page_number in seen_pages:
                continue
            seen_pages.add(page_number)
            texts.append(reader.pages[page_number - 1].extract_text() or "")
        return "\n".join(texts)
    except Exception:
        return ""


def _detect_table_pages(pdf_path, allow_visual_detection=True):
    """PDF内の表ページを検出する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        allow_visual_detection: 画像解析による表ページ検出を許可する場合はTrue。

    Returns:
        処理結果。
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
    except Exception:
        return []

    table_pages = []
    seen = set()
    extracted_texts = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        extracted_texts.append(text)
        for label, keyword in TABLE_PAGE_KEYWORDS:
            if keyword in text and _is_supported_table_page(text, label) and (label, index) not in seen:
                table_pages.append((label, index))
                seen.add((label, index))
    if not table_pages:
        table_pages.extend(_detect_garbled_table_pages(extracted_texts))
    if allow_visual_detection and not table_pages and _should_use_visual_table_detection(extracted_texts):
        table_pages.extend(_detect_table_pages_with_ai(pdf_path, len(reader.pages)))
    return _deduplicate_table_pages(table_pages)


def _detect_garbled_table_pages(extracted_texts):
    """文字化けした表ページ候補を検出する。

    Args:
        extracted_texts: PDF各ページから抽出したテキスト一覧。

    Returns:
        処理結果。
    """
    table_pages = []
    for index, text in enumerate(extracted_texts, start=1):
        normalized = unicodedata.normalize("NFKC", text or "")
        if _looks_like_garbled_fixture_table(normalized):
            table_pages.append(("建具表", index))
        elif _looks_like_garbled_finish_table(normalized):
            table_pages.append(("室内仕上表", index))
    return table_pages


def _looks_like_garbled_fixture_table(text):
    """文字化けテキストが建具表らしいかを返す。

    Args:
        text: 解析対象の文字列。

    Returns:
        処理結果。
    """
    return "਺ྔ" in text and "ੇ๏" in text and text.count("਺ྔ") >= 2


def _looks_like_garbled_finish_table(text):
    """文字化けテキストが仕上表らしいかを返す。

    Args:
        text: 解析対象の文字列。

    Returns:
        処理結果。
    """
    finish_markers = ("έΠΧϧ൘", "̥ɾ̗", "Լ԰", "্ද")
    floor_area_markers = ("̍֊চ໘ੵ", "̎֊চ໘ੵ", "Ԇচ໘ੵ")
    return any(marker in text for marker in finish_markers) and any(marker in text for marker in floor_area_markers)


def _should_use_visual_table_detection(extracted_texts):
    """画像解析による表ページ検出が必要かを返す。

    Args:
        extracted_texts: PDF各ページから抽出したテキスト一覧。

    Returns:
        処理結果。
    """
    text = "\n".join(extracted_texts)
    if not text.strip():
        return True
    normalized = unicodedata.normalize("NFKC", text)
    table_keyword_count = sum(normalized.count(keyword) for _label, keyword in TABLE_PAGE_KEYWORDS)
    related_keyword_count = sum(
        normalized.count(keyword)
        for keyword in ("面積", "部屋", "床", "居室", "仕上", "建具", "開口", "天井")
    )
    return table_keyword_count == 0 and related_keyword_count <= 10


def _detect_table_pages_with_ai(pdf_path, page_count):
    """AI画像解析で表ページを検出する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        page_count: PDFの総ページ数。

    Returns:
        処理結果。
    """
    if not _setting("OPENAI_API_KEY"):
        return []
    if str(_setting("OPENAI_VISUAL_TABLE_PAGE_DETECTION", "false")).lower() not in {"1", "true", "yes", "on"}:
        return []
    try:
        max_pages = int(_setting("OPENAI_TABLE_PAGE_DETECTION_MAX_PAGES", "60"))
    except (TypeError, ValueError):
        max_pages = 60
    if page_count > max_pages:
        return []

    try:
        from openai import OpenAI
    except ImportError:
        return []

    uploaded_file = None
    try:
        model = _setting("OPENAI_PDF_ANALYSIS_MODEL", "gpt-4o")
        client = OpenAI(api_key=_setting("OPENAI_API_KEY"))
        with open(pdf_path, "rb") as pdf_file:
            uploaded_file = client.files.create(file=pdf_file, purpose="user_data")
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded_file.id},
                        {"type": "input_text", "text": _table_page_detection_prompt(page_count)},
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "wallpaper_table_page_detection",
                    "strict": True,
                    "schema": _table_page_detection_schema(),
                }
            },
        )
        return _parse_table_page_detection_response(_response_text(response), page_count)
    except Exception:
        return []
    finally:
        if uploaded_file is not None:
            try:
                client.files.delete(uploaded_file.id)
            except Exception:
                pass


def _table_page_detection_prompt(page_count):
    """表ページ検出AIへ渡す指示文を返す。

    Args:
        page_count: PDFの総ページ数。

    Returns:
        処理結果。
    """
    labels = "、".join(label for label, _keyword in TABLE_PAGE_KEYWORDS)
    return f"""
添付PDFは建築図面です。PDF内の埋め込みテキストが文字化けしている可能性があるため、
ページ画像を目視/OCRするつもりで、壁紙積算に使う表ページを判定してください。

対象ページは1ページから{page_count}ページまでです。ページ番号はPDFビューア上の1始まりで返してください。

検出対象:
- {labels}

判定ルール:
- 図面名・表題欄・表の見出しを優先して判定してください。
- 「床面積表」「居室区画面積表」は部屋候補と面積の根拠になるため特に拾ってください。
- 「室内仕上表」「内部仕上表」は壁・天井・床などの仕上表であれば拾ってください。
- 「建具表」は開口部の根拠になる建具姿図、建具リスト、建具寸法表であれば拾ってください。
- 平面図、立面図、展開図、天井伏図だけのページは返さないでください。
- 確信度が低いページは含めず、confidence は0から1で返してください。
""".strip()


def _table_page_detection_schema():
    """表ページ検出AIのJSON応答スキーマを返す。

    Returns:
        処理結果。
    """
    return {
        "type": "object",
        "properties": {
            "table_pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "enum": [label for label, _keyword in TABLE_PAGE_KEYWORDS],
                        },
                        "page": {"type": "integer", "minimum": 1},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["label", "page", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["table_pages"],
        "additionalProperties": False,
    }


def _parse_table_page_detection_response(response_text, page_count):
    """表ページ検出AIの応答をページ一覧へ変換する。

    Args:
        response_text: AI応答のJSON文字列。
        page_count: PDFの総ページ数。

    Returns:
        処理結果。
    """
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return []

    table_pages = []
    supported_labels = {label for label, _keyword in TABLE_PAGE_KEYWORDS}
    for item in payload.get("table_pages", []):
        label = str(item.get("label") or "").strip()
        try:
            page = int(item.get("page"))
            confidence = Decimal(str(item.get("confidence", 0)))
        except Exception:
            continue
        if label not in supported_labels or page < 1 or page > page_count or confidence < Decimal("0.55"):
            continue
        table_pages.append((label, page))
    return _deduplicate_table_pages(table_pages)


def _deduplicate_table_pages(table_pages):
    """表ページ一覧の重複を除外する。

    Args:
        table_pages: 表ページのラベルとページ番号。

    Returns:
        処理結果。
    """
    deduplicated = []
    seen = set()
    for label, page in table_pages:
        key = (label, page)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append((label, page))
    return deduplicated


def _room_table_candidates(pdf_path, table_pages):
    """表ページから部屋候補を抽出する。

    Args:
        pdf_path: 解析対象のPDFファイルパス。
        table_pages: 表ページのラベルとページ番号。

    Returns:
        処理結果。
    """
    if not table_pages:
        return []
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
    except Exception:
        return []

    for preferred_label in ("居室区画面積表", "床面積表"):
        candidates = []
        for label, page_number in table_pages:
            if label != preferred_label:
                continue
            text = reader.pages[page_number - 1].extract_text() or ""
            candidates.extend(_room_table_candidates_from_text(text, label, page_number))
        candidates = [
            candidate
            for candidate in candidates
            if _normalize_room_name(candidate.name) and not _is_non_wallpaper_candidate(candidate.name)
        ]
        if candidates:
            return _normalize_room_table_candidates(candidates)
    return []


def _normalize_room_table_candidates(candidates):
    """部屋候補名と階数を正規化する。

    Args:
        candidates: 正規化または重複除外する部屋候補一覧。

    Returns:
        処理結果。
    """
    aggregated = []
    storage_by_floor = {}
    for candidate in candidates:
        if _normalize_room_name(candidate.name) == "収納":
            current = storage_by_floor.get(candidate.floor)
            area = candidate.area_m2 or Decimal("0")
            if current is None:
                storage_by_floor[candidate.floor] = RoomCandidate(
                    floor=candidate.floor,
                    name="収納 一式",
                    area_m2=area,
                    source=candidate.source,
                    page=candidate.page,
                )
            else:
                storage_by_floor[candidate.floor] = RoomCandidate(
                    floor=current.floor,
                    name=current.name,
                    area_m2=(current.area_m2 or Decimal("0")) + area,
                    source=current.source,
                    page=current.page,
                )
            continue
        aggregated.append(candidate)
    aggregated.extend(storage_by_floor.values())
    return _deduplicate_candidate_names(aggregated)


def _deduplicate_candidate_names(candidates):
    """同一候補名の重複を除外する。

    Args:
        candidates: 正規化または重複除外する部屋候補一覧。

    Returns:
        処理結果。
    """
    totals = {}
    for candidate in candidates:
        key = (_normalize_floor(candidate.floor), _normalize_room_name(candidate.name))
        totals[key] = totals.get(key, 0) + 1

    seen = {}
    renamed = []
    for candidate in candidates:
        key = (_normalize_floor(candidate.floor), _normalize_room_name(candidate.name))
        seen[key] = seen.get(key, 0) + 1
        name = candidate.name
        if totals[key] > 1:
            name = f"{candidate.name} {seen[key]}"
        renamed.append(
            RoomCandidate(
                floor=candidate.floor,
                name=name,
                area_m2=candidate.area_m2,
                source=candidate.source,
                page=candidate.page,
            )
        )
    return renamed


def _room_table_candidates_from_text(text, source, page_number):
    """表ページテキストから部屋候補を抽出する。

    Args:
        text: 解析対象の文字列。
        source: 階数やページ番号の判定に使う文字列。
        page_number: PDF上のページ番号。

    Returns:
        処理結果。
    """
    if source == "床面積表":
        return _floor_area_table_candidates(text, source, page_number)
    if source == "居室区画面積表":
        return _living_area_table_candidates(text, source, page_number)
    if source in {"室内仕上表", "内部仕上表"}:
        return _finish_table_candidates(text, source, page_number)
    return []


def _floor_area_table_candidates(text, source, page_number):
    """床面積表から部屋候補を抽出する。

    Args:
        text: 解析対象の文字列。
        source: 階数やページ番号の判定に使う文字列。
        page_number: PDF上のページ番号。

    Returns:
        処理結果。
    """
    section = _table_section(text, "床面積表")
    if not section:
        return []

    candidates = []
    floor = "1F"
    switch_after_1f_total = False
    for line in section.splitlines():
        if "1階小計" in line:
            switch_after_1f_total = True
            continue
        if "2階小計" in line or "延床面積" in line:
            break
        line_candidates = _room_candidates_from_line(line, floor, source, page_number)
        if switch_after_1f_total and line_candidates and not any(_normalize_room_name(candidate.name) == "PS" for candidate in line_candidates):
            floor = "2F"
            line_candidates = _room_candidates_from_line(line, floor, source, page_number)
            switch_after_1f_total = False
        candidates.extend(line_candidates)
        if switch_after_1f_total and any(_normalize_room_name(candidate.name) == "PS" for candidate in line_candidates):
            floor = "2F"
            switch_after_1f_total = False
    return candidates


def _living_area_table_candidates(text, source, page_number):
    """居室区画面積表から部屋候補を抽出する。

    Args:
        text: 解析対象の文字列。
        source: 階数やページ番号の判定に使う文字列。
        page_number: PDF上のページ番号。

    Returns:
        処理結果。
    """
    section = _table_section(text, "居室区画面積表")
    if not section:
        return []

    raw_candidates = []
    for line in section.splitlines():
        if "凡例" in line or "合計" in line:
            break
        raw_candidates.extend(_room_candidates_from_line(line, "", source, page_number))

    return _infer_floors_for_living_area_candidates(raw_candidates)


def _finish_table_candidates(text, source, page_number):
    """仕上表から部屋候補を抽出する。

    Args:
        text: 解析対象の文字列。
        source: 階数やページ番号の判定に使う文字列。
        page_number: PDF上のページ番号。

    Returns:
        処理結果。
    """
    candidates = []
    floor = ""
    for raw_line in text.splitlines():
        line = unicodedata.normalize("NFKC", raw_line).strip()
        floor_match = re.search(r"([1-9])\s*(?:F|階)", line, re.IGNORECASE)
        if floor_match:
            floor = f"{floor_match.group(1)}F"
        candidates.extend(_room_candidates_from_line(line, floor, source, page_number))
    return candidates


def _table_section(text, marker):
    """表テキストから指定見出し以降のセクションを取り出す。

    Args:
        text: 解析対象の文字列。
        marker: 抽出対象の表セクション見出し。

    Returns:
        処理結果。
    """
    start = text.find(marker)
    if start < 0:
        return ""
    return text[start:]


def _room_candidates_from_line(line, floor, source, page_number):
    """表の1行から部屋候補を抽出する。

    Args:
        line: 表ページから抽出した1行の文字列。
        floor: 階数ラベル。
        source: 階数やページ番号の判定に使う文字列。
        page_number: PDF上のページ番号。

    Returns:
        処理結果。
    """
    candidates = []
    line = unicodedata.normalize("NFKC", line)
    normalized = _normalize_text(line)
    if any(skip in normalized for skip in ("小計", "延床面積", "合計", "凡例", "計算式", "面積", "タイプ")):
        return candidates
    pattern = re.compile(r"([A-Za-zＡ-Ｚａ-ｚ一-龥ァ-ヶｦ-ﾟー０-９0-9]+(?:[ 　]*[A-Za-zＡ-Ｚａ-ｚ一-龥ァ-ヶｦ-ﾟー０-９0-9]+)*)\s+(\d+\.\d{3})")
    for match in pattern.finditer(line):
        name = _normalize_room_display_name(match.group(1))
        area = _decimal_from_ai(match.group(2), "0")
        if not name or area <= 0:
            continue
        candidates.append(RoomCandidate(floor=floor, name=name, area_m2=area, source=source, page=page_number))
    return candidates


def _infer_floors_for_living_area_candidates(candidates):
    """居室区画面積表の候補へ階数を補完する。

    Args:
        candidates: 正規化または重複除外する部屋候補一覧。

    Returns:
        処理結果。
    """
    if not candidates:
        return []
    inferred = []
    floor = "1F"
    for candidate in candidates:
        inferred.append(
            RoomCandidate(
                floor=floor,
                name=candidate.name,
                area_m2=candidate.area_m2,
                source=candidate.source,
                page=candidate.page,
            )
        )
        if floor == "1F" and _normalize_room_name(candidate.name) == "PS":
            floor = "2F"
    return inferred


def _normalize_room_display_name(value):
    """部屋候補の表示名を正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    name = re.sub(r"^[0-9０-９.,，×＝=\-+\s]+", "", name)
    name = name.translate(str.maketrans({
        "ﾎ": "ホ",
        "ｰ": "ー",
    }))
    replacements = {
        "ﾄｲﾚ": "トイレ",
        "ﾎｰﾙ": "ホール",
        "ﾊﾟﾝﾄﾘｰ": "パントリー",
        "ﾗﾝﾄﾞﾘｰﾙｰﾑ": "ランドリールーム",
        "ﾌｧﾐﾘｰｸﾛｰｾﾞｯﾄ": "ファミリークローゼット",
        "脱衣ﾗﾝﾄﾞﾘｰﾙｰﾑ": "脱衣ランドリールーム",
    }
    for source, target in replacements.items():
        name = name.replace(source, target)
    return name.strip()


def _is_supported_table_page(text, label):
    """部屋候補抽出に対応した表ページかどうかを返す。

    Args:
        text: 解析対象の文字列。
        label: 表ページ種別のラベル。

    Returns:
        処理結果。
    """
    normalized = unicodedata.normalize("NFKC", text)
    if label == "居室区画面積表":
        return "部 屋" in normalized or "部屋" in normalized
    if label == "床面積表":
        return "部屋" in normalized and ("小計" in normalized or "非居室" in normalized)
    if label in {"室内仕上表", "内部仕上表"}:
        return any(keyword in normalized for keyword in ("壁", "天井", "クロス", "仕上"))
    if label == "建具表":
        return any(keyword in normalized for keyword in ("建具", "開口", "姿図", "寸法"))
    return False


def _is_non_wallpaper_candidate(name):
    """壁紙施工対象外の部屋候補かどうかを返す。

    Args:
        name: 名前。

    Returns:
        処理結果。
    """
    normalized = _normalize_room_name(name)
    return any(_normalize_room_name(excluded) in normalized for excluded in NON_WALLPAPER_CANDIDATE_NAMES)


def _missing_room_count(missing_rooms, analyzed_rooms):
    """抽出失敗として追加する部屋数を数える。

    Args:
        missing_rooms: 抽出できなかった部屋名の一覧。
        analyzed_rooms: AI解析済みの部屋一覧。

    Returns:
        処理結果。
    """
    extracted = set()
    for room in analyzed_rooms:
        extracted.update(_room_match_keys(room.name))
    seen = set()
    for room_name in missing_rooms or []:
        normalized = _normalize_room_name(room_name)
        if not normalized or _room_match_keys(room_name) & extracted:
            continue
        seen.add(normalized)
    return len(seen)


def _candidate_missing_room_count(rooms, missing_rooms, room_candidates):
    """表ページ候補から未表示の部屋数を数える。

    Args:
        rooms: 抽出済みの部屋情報。
        missing_rooms: 抽出できなかった部屋名の一覧。
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        処理結果。
    """
    displayed = set()
    storage_bundle_floors = set()
    for room in rooms:
        displayed.update(_displayed_room_match_keys(room.name, room.note))
        if _is_storage_bundle(room.name):
            floor = _floor_label_from_text(f"{room.name} {room.note}")
            if floor:
                storage_bundle_floors.add(floor)
    for room_name in missing_rooms or []:
        displayed.update(_displayed_room_match_keys(room_name))
    return sum(
        1
        for candidate in room_candidates
        if _normalize_room_name(candidate.name)
        and not _candidate_is_displayed(candidate, displayed, storage_bundle_floors)
    )


def _normalize_room_name(value):
    """部屋名を照合用に正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    return unicodedata.normalize("NFKC", str(value or "")).upper().replace(" ", "")


def _normalize_floor(value):
    """階数表記を照合用に正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    normalized = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    match = re.search(r"([1-9])\s*(?:F|階)", normalized)
    return f"{match.group(1)}F" if match else normalized


def _room_match_keys(value):
    """部屋名の照合キーを作成する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    normalized = _normalize_room_name(value)
    keys = {normalized} if normalized else set()
    without_floor = re.sub(r"^[1-9](?:F|階)", "", normalized)
    has_floor = without_floor != normalized
    if without_floor and not has_floor:
        keys.add(without_floor)
    return keys


def _candidate_match_keys(candidate):
    """部屋候補の照合キーを作成する。

    Args:
        candidate: 部屋候補。

    Returns:
        処理結果。
    """
    keys = set()
    if candidate.floor:
        keys.add(_normalize_room_name(f"{candidate.floor} {candidate.name}"))
    else:
        keys.update(_room_match_keys(candidate.name))
    return keys


def _displayed_room_match_keys(name, note=""):
    """表示済み部屋の照合キーを作成する。

    Args:
        name: 名前。
        note: 備考。

    Returns:
        処理結果。
    """
    keys = _room_match_keys(name)
    floor = _floor_label_from_text(f"{name} {note}")
    if floor:
        keys.add(_normalize_room_name(f"{floor} {name}"))
    return keys


def _candidate_is_displayed(candidate, displayed, storage_bundle_floors):
    """部屋候補が表示済みとして扱えるかを返す。

    Args:
        candidate: 部屋候補。
        displayed: 画面に表示済みの部屋照合キー。
        storage_bundle_floors: 収納一式として表示済みの階数。

    Returns:
        処理結果。
    """
    if _candidate_match_keys(candidate) & displayed:
        return True
    if (
        _normalize_room_name(candidate.name) == "収納"
        and candidate.floor
        and _normalize_floor(candidate.floor) in storage_bundle_floors
    ):
        return True
    return False


def _is_storage_bundle(value):
    """収納一式を表す部屋名かどうかを返す。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    normalized = _normalize_room_name(value)
    return "収納" in normalized and "一式" in normalized


def _floor_label_from_text(value):
    """文字列から階数ラベルを抽出する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
    match = re.search(r"([1-9])\s*(?:F|階)", normalized)
    return f"{match.group(1)}F" if match else ""


def _expected_room_counts(plan_text):
    """平面図テキストから部屋種別ごとの候補数を推定する。

    Args:
        plan_text: 平面図または天井伏図から抽出したテキスト。

    Returns:
        処理結果。
    """
    text = _normalize_text(plan_text)
    counts = {}
    for label, aliases in ROOM_LABEL_PATTERNS.items():
        count = _count_alias_occurrences(text, aliases)
        if count:
            counts[label] = count
    for excluded in NON_WALLPAPER_ROOM_LABELS:
        counts.pop(excluded, None)
    return counts


def _actual_room_count(room_text, aliases, expected_count):
    """抽出済み部屋テキスト内の部屋種別件数を数える。

    Args:
        room_text: 抽出済み部屋名を連結したテキスト。
        aliases: 部屋種別の別名一覧。
        expected_count: 期待される部屋数。

    Returns:
        処理結果。
    """
    text = _normalize_text(room_text)
    if "一式" in str(room_text) and any(alias in text for alias in {_normalize_text(alias) for alias in aliases}):
        return expected_count
    return _count_alias_occurrences(text, aliases)


def _count_alias_occurrences(text, aliases):
    """別名一覧に一致する出現数を数える。

    Args:
        text: 解析対象の文字列。
        aliases: 部屋種別の別名一覧。

    Returns:
        処理結果。
    """
    normalized_aliases = sorted(
        {_normalize_text(alias) for alias in aliases if _normalize_text(alias)},
        key=len,
        reverse=True,
    )
    spans = []
    for alias in normalized_aliases:
        for match in re.finditer(re.escape(alias), text):
            span = match.span()
            if any(span[0] < existing[1] and existing[0] < span[1] for existing in spans):
                continue
            spans.append(span)
    return len(spans)


def _normalize_text(value):
    """照合しやすいよう文字列を正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    return str(value).replace("ＬＤＫ", "LDK").upper()


def _decimal_from_ai(value, default):
    """AI応答値をDecimalへ変換する。

    Args:
        value: 変換または正規化する値。
        default: 値が空または不正な場合の既定値。

    Returns:
        処理結果。
    """
    try:
        return Decimal(str(value if value is not None else default)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal(default)


def _setting(name, default=None):
    """Django設定または環境変数から値を取得する。

    Args:
        name: 名前。
        default: 値が空または不正な場合の既定値。

    Returns:
        処理結果。
    """
    if settings is not None and getattr(settings, "configured", False):
        value = getattr(settings, name, None)
        if value is not None:
            return value
    return os.environ.get(name, default)


def _sample_plan_rooms(parsed_pages=None):
    """テスト用サンプルPDFから期待される部屋一覧を返す。

    Args:
        parsed_pages: 検証済みのページ指定。

    Returns:
        処理結果。
    """
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
