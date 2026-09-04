"""Read a Japanese request with the catalogue's English vocabulary.

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
    ("取付板", "bracket"),
    ("取り付け板", "bracket"),
    ("マウント", "mount"),
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
