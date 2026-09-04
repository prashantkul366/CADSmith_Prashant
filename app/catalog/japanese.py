"""Read Japanese CAD language with the English vocabulary the app matches on.

Two callers, one technique. ``to_english`` serves the catalogue router;
``to_english_instruction`` serves the natural-language editor. Both turn a
Japanese sentence into a lookup key for patterns that were written in
English, and neither is a translation: the result is often not a sentence,
and is never shown to anyone or sent to a model.

## The catalogue

The router recognises a standard part by English keyword - "washer", "20mm
bore", "24 teeth".  A Japanese request carries exactly the same information
and matches none of it, so without this a Japanese user loses the whole
catalogue: every fastener, bearing, gear and o-ring stops being exact and
instant and goes to the model instead.

Rather than teach every regex in ``parts.py`` and ``router.py`` a second
language, one pass rewrites the request into the vocabulary those regexes
already speak, and the router tries the rewrite only when the original found
nothing.  Two consequences worth being clear about:

* **Nothing here changes what the model is sent.**  The rewrite is used to
  look up a catalogue part and then discarded; a request that falls through
  reaches the Planner exactly as it was typed.
* **A false match is worse than no match.**  The refusals are translated
  first and most carefully: 「Oリング用の溝」 is a groove, not an o-ring, and
  handing back the o-ring is the silent substitution the whole catalogue
  guard exists to stop.  Every custom-context word in ``parts.py`` has a
  Japanese counterpart here, and they are checked before anything else.

Word order is the other half of the problem.  Japanese writes the label in
front of the measurement - 内径20mm - and English behind it - "20mm inside
diameter".  So labelled measurements are moved, not just translated.
"""

from __future__ import annotations

import re
import unicodedata

#: Any of these means the request wants a part that *receives* a standard
#: part, not the standard part itself. Each maps to a phrase already in
#: ``parts._CUSTOM_CONTEXT``, so the existing guard fires unchanged.
CUSTOM_CONTEXT = (
    ("ハウジング", "housing"),
    ("筐体", "enclosure"),
    ("エンクロージャ", "enclosure"),
    ("ブラケット", "bracket"),
    # 取付 covers every compound a mounting part is written as - 取付板,
    # 取付プレート, 取付座, 取付ブラケット - which the individual entries did
    # not: a live run substituted an actual M8 screw for 「M8 のボルト 4 本を
    # 通す取付プレート」, the exact silent substitution this guard exists to
    # stop. English caught the same request on "mount" in "mounting plate".
    ("取付", "mount"),
    ("取り付け", "mount"),
    ("台座", "mount"),
    ("土台", "mount"),
    ("架台", "mount"),
    ("マウント", "mount"),
    ("ケース", "enclosure"),
    # A part something else passes through is not that something else.
    ("通す", "clearance for"),
    ("通る", "clearance for"),
    ("貫通", "clearance for"),
    # The catalogue holds no plates, so reading 板 or プレート as a custom
    # part cannot cost a standard one - and 板厚 is consumed as a measurement
    # before this scan runs.
    ("プレート", "plate for"),
    ("板", "plate for"),
    ("ホルダ", "holder"),
    ("ホルダー", "holder"),
    ("キャリア", "carrier"),
    ("マニホールド", "manifold"),
    ("アダプタ", "adapter"),
    ("アダプター", "adapter"),
    ("治具", "jig"),
    ("ジグ", "jig"),
    ("プーラ", "puller"),
    # A feature cut *for* a standard part is not that part.
    ("溝", "groove for"),
    ("みぞ", "groove for"),
    ("ザグリ", "counterbore for"),
    ("座ぐり", "counterbore for"),
    ("ぬすみ", "clearance for"),
    ("逃げ", "clearance for"),
    ("凹み", "pocket for"),
    ("ポケット", "pocket for"),
    ("窪み", "recess for"),
    ("切り欠き", "cutout for"),
    ("切欠き", "cutout for"),
    ("貫通穴", "hole for"),
    ("下穴", "hole for"),
    ("カバー", "cover for"),
    ("蓋", "cover for"),
)

#: Measurements. Japanese puts the label in front of the number and English
#: puts it behind, so each entry carries the English template rather than a
#: word: ``{n}`` is where the number lands.
MEASURES = (
    ("外径", "{n}mm outside diameter"),
    ("内径", "{n}mm inside diameter"),
    # One Japanese word covers both a spring's wire and an o-ring's cord, and
    # the two English patterns are separate. Emitting both costs nothing: the
    # spring reads only "wire" and the o-ring only "cord".
    ("線径", "{n}mm wire {n}mm cord"),
    ("断面径", "{n}mm cord"),
    ("自由長", "{n}mm free length"),
    ("全長", "{n}mm long"),
    ("長さ", "{n}mm long"),
    ("厚さ", "{n}mm thick"),
    ("厚み", "{n}mm thick"),
    ("板厚", "{n}mm thick"),
    ("歯幅", "{n}mm face"),
    ("幅", "{n}mm wide"),
    ("直径", "{n}mm diameter"),
    ("穴径", "{n}mm bore"),
    ("軸穴", "{n}mm bore"),
    ("ボア", "{n}mm bore"),
    ("径", "{n}mm diameter"),
)

#: Counts and moduli, which take no millimetre unit. ``module`` keeps the
#: label in front because that is the form ``router._MODULE`` reads.
COUNTS = (
    ("歯数", "{n} teeth"),
    ("モジュール", "module {n}"),
    ("有効巻数", "{n} coils"),
    ("巻数", "{n} coils"),
)

#: Plain nouns and qualifiers. Longest first throughout: 平歯車 must win over
#: 歯車, and 六角穴付きボルト over ボルト.
TERMS = (
    # fasteners
    ("六角穴付きボルト", "socket head cap screw"),
    ("六角穴付きねじ", "socket head cap screw"),
    ("キャップスクリュー", "socket head cap screw"),
    ("キャップボルト", "socket head cap screw"),
    ("皿ねじ", "countersunk screw"),
    ("皿頭", "countersunk"),
    ("なべねじ", "pan head screw"),
    ("なべ頭", "pan head"),
    ("ボタンボルト", "button head screw"),
    ("六角ボルト", "hex head bolt"),
    ("六角頭", "hex head"),
    ("止めねじ", "set screw"),
    ("いもねじ", "set screw"),
    ("イモネジ", "set screw"),
    ("袋ナット", "cap nut"),
    ("フランジナット", "flange nut"),
    ("四角ナット", "square nut"),
    ("六角ナット", "hex nut"),
    ("ナット", "nut"),
    ("ボルト", "bolt"),
    ("小ねじ", "machine screw"),
    ("ねじ", "screw"),
    ("ネジ", "screw"),
    # washers, seals, pins
    ("平座金", "flat washer"),
    ("座金", "washer"),
    ("ワッシャー", "washer"),
    ("ワッシャ", "washer"),
    ("オーリング", "o-ring"),
    ("Oリング", "o-ring"),
    ("平行ピン", "dowel pin"),
    ("ダウエルピン", "dowel pin"),
    ("ダウエル", "dowel"),
    ("ノックピン", "dowel pin"),
    # bearings
    ("深溝玉軸受", "deep groove ball bearing"),
    ("玉軸受", "ball bearing"),
    ("軸受", "bearing"),
    ("ベアリング", "bearing"),
    # gears
    ("平歯車", "spur gear"),
    ("はすば歯車", "helical gear"),
    ("ハスバ歯車", "helical gear"),
    ("やまば歯車", "herringbone gear"),
    ("かさ歯車", "bevel gear"),
    ("傘歯車", "bevel gear"),
    ("内歯車", "ring gear"),
    ("ラックギヤ", "rack gear"),
    ("ラックギア", "rack gear"),
    ("ラック", "rack"),
    ("ピニオン", "pinion"),
    ("歯車", "gear"),
    ("ギヤ", "gear"),
    ("ギア", "gear"),
    ("スプロケット", "sprocket"),
    # pulleys and belts
    ("タイミングプーリー", "timing pulley"),
    ("タイミングプーリ", "timing pulley"),
    ("プーリー", "pulley"),
    ("プーリ", "pulley"),
    # springs
    ("圧縮ばね", "compression spring"),
    ("圧縮コイルばね", "compression spring"),
    ("圧縮スプリング", "compression spring"),
    ("コイルばね", "helical spring"),
    ("ばね", "spring"),
    ("バネ", "spring"),
    ("スプリング", "spring"),
    # qualifiers the router reads
    ("ねじ付き", "threaded"),
    ("おねじ", "threaded"),
    ("ミリ", "mm"),
    ("ベルト", "belt"),
)

#: Particles and punctuation, which carry grammar rather than information.
#: Dropped to whitespace so a designation is not left glued to the next word.
PARTICLES = "のをがはにでともやへ、。・「」（）：:"

_JAPANESE = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")

#: Built once: a label with its number after it (内径20mm) or before it
#: (20mm内径), for every entry in MEASURES and COUNTS.
_NUMBER = r"(\d+(?:\.\d+)?)"


def has_japanese(text: str) -> bool:
    """True when the text contains kana or kanji.

    Latin-only text is left completely alone, so an English request never
    passes through the rewrite and cannot be changed by it.
    """
    return bool(_JAPANESE.search(text or ""))


def spaced(text: str) -> str:
    """The same text with a space between kana/kanji and any Latin neighbour.

    Python's ``\\b`` treats kanji as word characters, so a designation written
    flush against one - 6203軸受, NEMA 17ステッピング - has no word boundary
    after it and every ``\\b``-anchored pattern in this package misses it.
    Padding the seam restores the boundary without altering a character.
    """
    padded = re.sub(r"(?<=[぀-ヿ㐀-䶿一-鿿])(?=[0-9A-Za-z])", " ", text or "")
    return re.sub(r"(?<=[0-9A-Za-z])(?=[぀-ヿ㐀-䶿一-鿿])", " ", padded)


def to_english(text: str) -> str:
    """The same request, in the vocabulary the catalogue's patterns read.

    Not a translation - a lookup key. The result is often not a sentence, and
    is never shown to anyone or sent to a model.
    """
    # Full-width digits and letters are ordinary in Japanese input; NFKC folds
    # ２０ｍｍ to 20mm so one set of patterns handles both.
    # Not spaced() first: padding the seams would split 「Oリング」 into
    # 「O リング」 and the term table, which matches whole words, would then
    # miss it. Replacing the terms inserts the spaces this needs anyway.
    out = unicodedata.normalize("NFKC", text or "")

    for japanese, template in MEASURES + COUNTS:
        unit = r"\s*(?:mm)?" if template.count("mm") else ""
        # 内径20mm -> "20mm inside diameter"
        out = re.sub(japanese + r"\s*[:：]?\s*" + _NUMBER + unit,
                     lambda m, tpl=template: " " + tpl.format(n=m.group(1)) + " ",
                     out)
        # 20mm内径 -> the same
        out = re.sub(_NUMBER + unit + r"\s*(?:の)?\s*" + japanese,
                     lambda m, tpl=template: " " + tpl.format(n=m.group(1)) + " ",
                     out)

    for japanese, english in TERMS:
        out = out.replace(japanese, f" {english} ")

    # Scanned after the nouns have gone, not before: 深溝玉軸受 is a deep
    # *groove* ball bearing, and a scan of the raw text would read that 溝 as
    # a groove someone asked to be cut and refuse a bearing the catalogue has.
    flags = [english for japanese, english in CUSTOM_CONTEXT if japanese in out]

    out = "".join(" " if ch in PARTICLES else ch for ch in out)
    # Any designation still glued to a kanji - 6203軸受 where 軸受 was not in
    # the table - needs its word boundary back before the English patterns run.
    out = spaced(out)
    if flags:
        out = " ".join(flags) + " " + out
    return " ".join(out.split())


# ---------------------------------------------------------------------------
# The natural-language editor
# ---------------------------------------------------------------------------

#: What an edit instruction says, in the words ``server/edits.py`` matches on.
#: A different table from ``TERMS`` because the editor reads different things:
#: it wants the dimension noun (``thickness``), the direction (``increase``),
#: and above all the words that mean *new geometry* - a rib, a fillet, a
#: pocket - because those must be refused as parameter patches and handed to
#: the Refiner instead. Getting 「補強リブを追加する」 wrong in the other
#: direction would patch some unrelated number and call it a rib.
#:
#: Longest first: 肉厚 must win over 厚, and 面取り over 取り.
EDIT_TERMS = (
    # dimensions, in the canonical form edits._SYNONYMS already knows
    ("外径", "outer diameter"),
    ("内径", "inner diameter"),
    ("肉厚", "wall thickness"),
    ("壁厚", "wall thickness"),
    ("板厚", "thickness"),
    ("厚さ", "thickness"),
    ("厚み", "thickness"),
    ("直径", "diameter"),
    ("半径", "radius"),
    ("全長", "length"),
    ("長さ", "length"),
    ("高さ", "height"),
    ("深さ", "depth"),
    ("軸穴", "bore"),
    ("ボア", "bore"),
    ("穴数", "hole count"),
    ("個数", "count"),
    ("歯数", "teeth"),
    ("歯幅", "face width"),
    ("線径", "wire diameter"),
    ("自由長", "free length"),
    ("モジュール", "module"),
    ("ピッチ", "pitch"),
    ("角度", "angle"),
    ("公差", "tolerance"),
    ("間隔", "spacing"),
    ("フランジ", "flange"),
    ("ハブ", "hub"),
    ("幅", "width"),
    ("穴", "hole"),
    ("径", "diameter"),
    # new geometry: these have to survive into the English so the editor
    # refuses the patch and calls the Refiner
    ("補強リブ", "reinforcing rib"),
    ("リブ", "rib"),
    ("ガセット", "gusset"),
    ("面取り", "chamfer"),
    ("フィレット", "fillet"),
    ("角丸", "fillet"),
    ("ねじ山", "thread"),
    ("ローレット", "knurl"),
    ("テーパ", "taper"),
    ("スロット", "slot"),
    ("長穴", "slot"),
    ("ポケット", "pocket"),
    ("ボス", "boss"),
    ("シェル", "shell"),
    ("肉抜き", "shell"),
    ("ミラー", "mirror"),
    ("回転", "rotate"),
    ("押し出し", "extrude"),
    ("ロフト", "loft"),
    ("スイープ", "sweep"),
    # direction
    ("増やす", "increase"),
    ("増やして", "increase"),
    ("減らす", "reduce"),
    ("減らして", "reduce"),
    ("大きく", "larger"),
    ("小さく", "smaller"),
    ("太く", "widen"),
    ("細く", "thinner"),
    ("厚く", "thicken"),
    ("薄く", "thinner"),
    ("長く", "lengthen"),
    ("短く", "shorten"),
    ("高く", "taller"),
    ("低く", "shorter"),
    ("追加", "add"),
    ("足して", "add"),
    ("削除", "remove"),
    ("除去", "remove"),
    ("なくす", "remove"),
    ("移動", "move"),
    ("変更", "change"),
    ("にする", "set to"),
    ("してください", " "),
    ("します", " "),
    ("して", " "),
    ("する", " "),
    ("ミリ", "mm"),
    ("度", "degrees"),
)

#: Counter suffixes. 「6 個の穴」 counts holes; left in place the 個 would sit
#: between the number and the noun and push them apart, and the editor picks
#: the number nearest the parameter it matched.
COUNTERS = ("個", "本", "枚", "箇所", "つ", "カ所", "ヶ所")


def to_english_instruction(text: str) -> str:
    """An edit instruction in the vocabulary ``server/edits.py`` matches on.

    Word order is largely preserved, because the editor resolves an ambiguous
    request by taking the number nearest the parameter it matched: 「厚さを
    5mm にする」 has to come out with the 5 still beside the thickness.
    """
    out = unicodedata.normalize("NFKC", text or "")
    for japanese, english in EDIT_TERMS:
        out = out.replace(japanese, f" {english} ")
    for counter in COUNTERS:
        out = out.replace(counter, " ")
    out = "".join(" " if ch in PARTICLES else ch for ch in out)
    return " ".join(spaced(out).split())
