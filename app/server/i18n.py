"""Server messages, in the language the person asked for.

Two rules divide this file from everything around it.

**Only text a person reads is here.**  The Refiner is told what the kernel
measured in English, because the instructions it is steered by are written in
English, and translating them would change what the pipeline does rather than
what it says.  ``spec.SpecReport.feedback`` and the agents' prompts are model
input and stay where they are.  So does the run log, which carries lines
autofab itself emits.

**English is the fallback, never an error.**  A key with no Japanese entry
falls back to English rather than raising: a gap in the catalogue should show
as untranslated text, not as a failed run.  ``check()`` exists so the test
suite can fail the build on that gap instead of a user finding it.

The measured checks are deliberately absent.  Each one carries a stable
``key`` to the browser, which looks up its own label; sending translated
labels would mean the server having to know a language the browser has
already chosen.
"""

from __future__ import annotations

from typing import Optional

DEFAULT_LANG = "en"
LANGS = ("en", "ja")

#: Every message keyed by id, then by language.  Placeholders are named, so a
#: translation may put them in a different order - which Japanese usually
#: needs, since the verb goes last and the qualifier goes first.
MESSAGES: dict[str, dict[str, str]] = {

    # -- the Planner found nothing to build --------------------------------
    "plan.empty": {
        "en": "The Planner did not find a part to make in this request: it "
              "returned no components and no overall size. Describe a part - "
              "its shape, size and features.",
        "ja": "このリクエストから作成する部品を特定できませんでした。"
              "構成要素も全体寸法も返されていません。形状・寸法・特徴を"
              "含めて部品を説明してください。",
    },
    "plan.empty.note": {
        "en": ' The Planner noted: "{note}"',
        "ja": "（プランナーの補足: 「{note}」）",
    },
    "plan.prose": {
        "en": "The Planner replied with prose instead of a design plan, "
              "which usually means the model did not treat this as a request "
              "for a physical part. Describe a part to make - its shape, "
              "size and features.",
        "ja": "プランナーが設計プランではなく文章で応答しました。多くの"
              "場合、これはモデルがこの入力を物理部品の要求として扱わな"
              "かったことを意味します。形状・寸法・特徴を含めて、作成する"
              "部品を説明してください。",
    },
    "plan.prose.said": {
        "en": ' The model said: "{said}"',
        "ja": "（モデルの応答: 「{said}」）",
    },

    # -- the spend ceiling --------------------------------------------------
    "budget.stopped": {
        "en": "This run reached its {limit}-token budget after {calls} model "
              "calls ({spent} tokens), so it was stopped before spending "
              "more. The attempts it did produce are still here. Raise "
              "CADSMITH_TOKEN_BUDGET if this part genuinely needs more, or "
              "lower the refinement iterations.",
        "ja": "この実行は {calls} 回のモデル呼び出し（{spent} トークン）で"
              "上限 {limit} トークンに達したため、これ以上消費する前に停止"
              "しました。ここまでの試行は残っています。この部品に本当に"
              "追加の予算が必要な場合は CADSMITH_TOKEN_BUDGET を引き上げる"
              "か、改良の反復回数を減らしてください。",
    },

    # -- the run ------------------------------------------------------------
    "job.samemodel": {
        "en": "Generation and judging both use {model}, so the Judge is "
              "grading its own work. Pick a different judge model for an "
              "independent check.",
        "ja": "生成と検証の両方に {model} が指定されているため、モデルが"
              "自分の出力を自分で採点することになります。独立した検証の"
              "ためには、別の検証モデルを選んでください。",
    },
    "job.catalogskipped": {
        "en": "Catalogue lookup skipped: {error}",
        "ja": "カタログ照会をスキップしました: {error}",
    },
    "job.catalogunbuildable": {
        "en": "The catalogue part would not build here, so the pipeline will "
              "generate it instead: {error}",
        "ja": "カタログ部品をこの環境で構築できなかったため、パイプラインで"
              "生成します: {error}",
    },

    # -- the catalogue path -------------------------------------------------
    "catalog.served": {
        "en": "{title} - served from the catalogue, not generated",
        "ja": "{title} — 生成ではなくカタログから提供されました",
    },
    "catalog.built": {
        "en": "Built in {ms} ms with no model call. Edit it like any other "
              "part - it is parametric source.",
        "ja": "モデルを呼び出さずに {ms} ミリ秒で構築しました。パラメトリック"
              "なソースなので、他の部品と同じように編集できます。",
    },

    # -- edits --------------------------------------------------------------
    "edit.nocontext": {
        "en": "That run cannot be edited in this session.",
        "ja": "この実行は、現在のセッションでは編集できません。",
    },
    "edit.notapplied": {
        "en": "Not applied - {reason}",
        "ja": "適用しませんでした - {reason}",
    },
    "edit.needsrefiner": {
        "en": "That is not a parameter change ({reason}), so it needs the "
              "Refiner agent - but no model backend is available: {problem} "
              "Try naming a dimension the script declares.",
        "ja": "これはパラメータの変更ではなく（{reason}）、リファイナー"
              "エージェントが必要ですが、モデルのバックエンドが利用でき"
              "ません: {problem} スクリプトが宣言している寸法名を指定して"
              "みてください。",
    },
    "edit.askingrefiner": {
        "en": "Not a parameter change ({reason}) - asking the Refiner agent.",
        "ja": "パラメータの変更ではありません（{reason}）。リファイナー"
              "エージェントに依頼します。",
    },
    "edit.reverted": {
        "en": "Not applied - the Refiner returned version {reverted} "
              "unchanged instead of editing version {current}, which would "
              "have undone your earlier edits. Version {current} is still "
              "the latest. Try naming the dimension you want changed.",
        "ja": "適用しませんでした。リファイナーはバージョン {current} を"
              "編集せず、バージョン {reverted} をそのまま返しました。"
              "そのまま記録すると、これまでの編集が取り消されてしまいます。"
              "最新はバージョン {current} のままです。変更したい寸法の名前を"
              "指定してみてください。",
    },
    "edit.unbuildable": {
        "en": "That change could not be built: {error_type}. The previous "
              "version is unchanged.",
        "ja": "この変更は構築できませんでした: {error_type}。直前の"
              "バージョンは変更されていません。",
    },
    "edit.failed": {
        "en": "The edit failed: {error}",
        "ja": "編集に失敗しました: {error}",
    },

    # -- refused requests ---------------------------------------------------
    "http.needprompt": {
        "en": "A prompt is required.",
        "ja": "プロンプトを入力してください。",
    },
    "http.promptlong": {
        "en": "Prompt is too long.",
        "ja": "プロンプトが長すぎます。",
    },
    "http.nocadquery": {
        "en": "CadQuery is not available in this environment: {detail}",
        "ja": "この環境では CadQuery を利用できません: {detail}",
    },
    "http.nojob": {
        "en": "No such job.",
        "ja": "該当するジョブがありません。",
    },
    "http.nothingtoedit": {
        "en": "That run produced nothing to edit.",
        "ja": "この実行には編集できる結果がありません。",
    },
    "http.stillworking": {
        "en": "That run is still working; wait for it to finish.",
        "ja": "この実行はまだ処理中です。完了までお待ちください。",
    },
    "http.needinstruction": {
        "en": "An instruction is required.",
        "ja": "変更内容を入力してください。",
    },
    "http.instructionlong": {
        "en": "Instruction is too long.",
        "ja": "変更内容が長すぎます。",
    },
    "http.norebuild": {
        "en": "CadQuery is not available, so nothing can be rebuilt.",
        "ja": "CadQuery が利用できないため、再構築できません。",
    },
    "http.badversion": {
        "en": "Invalid version.",
        "ja": "バージョンの指定が不正です。",
    },
    "http.noversion": {
        "en": "No such version.",
        "ja": "該当するバージョンがありません。",
    },
    "http.noreplay": {
        "en": "That run has no recorded events or geometry to replay.",
        "ja": "この実行には、再生できるイベントや形状が記録されていません。",
    },
    "http.noprovider": {
        "en": "Unknown provider.",
        "ja": "不明なプロバイダーです。",
    },
    "http.valuelong": {
        "en": "Value is too long.",
        "ja": "入力値が長すぎます。",
    },
    "http.noartifact": {
        "en": "Unknown artifact.",
        "ja": "不明なアーティファクトです。",
    },
    "http.artifactmissing": {
        "en": "Artifact not available.",
        "ja": "アーティファクトが見つかりません。",
    },
    "http.nodrawing": {
        "en": "Could not build the drawing: {error}",
        "ja": "図面を作成できませんでした: {error}",
    },
    "http.jsonbody": {
        "en": "Expected a JSON body.",
        "ja": "JSON 形式の本文が必要です。",
    },
    "http.jsonobject": {
        "en": "Expected a JSON object.",
        "ja": "JSON オブジェクトが必要です。",
    },
    "http.nofrontend": {
        "en": "Frontend is not built.",
        "ja": "フロントエンドがビルドされていません。",
    },
}


def normalise(lang: Optional[str]) -> str:
    """The nearest language this app has, for anything a client sends.

    Accepts a bare tag or a region-qualified one - ``ja``, ``ja-JP``, ``JA``
    all mean Japanese - and falls back to English rather than refusing.
    """
    if not lang:
        return DEFAULT_LANG
    code = str(lang).strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGS else DEFAULT_LANG


def from_header(accept_language: Optional[str]) -> str:
    """The first language in an ``Accept-Language`` header that we speak.

    Quality values are honoured only in the order they arrive, which is what
    browsers send anyway; a full q-sort would be precision this does not need.
    """
    if not accept_language:
        return DEFAULT_LANG
    for part in str(accept_language).split(","):
        tag = part.split(";")[0].strip()
        code = normalise(tag)
        if code != DEFAULT_LANG or tag.lower().startswith("en"):
            return code
    return DEFAULT_LANG


def t(key: str, lang: Optional[str] = None, **params) -> str:
    """One message, in ``lang``, with its placeholders filled in.

    An unknown key returns the key itself rather than raising: a message that
    reads oddly is a smaller failure than a run that stops.
    """
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    code = normalise(lang)
    text = entry.get(code) or entry[DEFAULT_LANG]
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, IndexError):
        # A placeholder the caller did not supply: show the message rather
        # than losing it to a formatting error.
        return text


def check() -> list[str]:
    """Keys that are missing a translation, for the test suite to fail on."""
    missing = []
    for key, entry in MESSAGES.items():
        for lang in LANGS:
            if not entry.get(lang):
                missing.append(f"{key}:{lang}")
    return missing
