"use strict";
/* ═══════════════════════════════════════════════════════════════════════
   CADSmith — interface language.

   Two languages, one dictionary, and a hard rule: nothing the model reads
   passes through here.  The Refiner is told what the kernel measured in
   English because that is the language its instructions are written in, and
   translating the text an agent is steered by would change what the pipeline
   does rather than what it looks like.  Everything a person reads is a key.

   Japanese is not a veneer over English word order.  Where a literal
   translation would read as machine output — "Converged after 3 iterations"
   — the Japanese says what a Japanese engineer would say, and the two
   strings are allowed to differ in structure.  Placeholders are named for
   that reason: {n} can sit anywhere in the sentence.
   ═══════════════════════════════════════════════════════════════════════ */

const I18N = (function () {

  const STORE_KEY = "cadsmith:lang";

  /* The languages offered, in the order the switch shows them. `label` is
     written in the language itself: someone who cannot read the current
     interface still has to be able to find their own. */
  const LANGS = [
    { code: "en", label: "EN", name: "English" },
    { code: "ja", label: "日本語", name: "Japanese" },
  ];

  const DICT = {

    /* ── chrome ─────────────────────────────────────────────────────── */
    "app.title":        ["CADSmith — Multi-Agent CAD Generation",
                         "CADSmith — マルチエージェント CAD 生成"],
    "app.engine":       ["Planner · Coder · Executor · Validator · Refiner",
                         "プランナー · コーダー · 実行 · 検証 · リファイナー"],
    "app.history":      ["History", "履歴"],
    "app.environment":  ["Environment", "実行環境"],
    "app.language":     ["Interface language", "表示言語"],

    "health.checking":  ["CHECKING…", "確認中…"],
    "health.unreachable": ["SERVER UNREACHABLE", "サーバーに接続できません"],
    "health.ready":     ["ALL SYSTEMS READY", "すべて正常"],
    "health.degraded":  ["DEGRADED", "一部機能が利用不可"],
    "health.notready":  ["NOT READY", "利用できません"],

    /* ── left column ────────────────────────────────────────────────── */
    "input.heading":    ["Design Input", "設計入力"],
    "input.describe":   ["Describe part", "部品を説明する"],
    "input.placeholder": ["Describe the part you want to generate…",
                          "生成したい部品を説明してください…"],
    "input.generate":   ["Generate CAD", "CAD を生成"],
    "input.samples":    ["Benchmark prompts", "ベンチマーク例"],

    "opt.iterations":   ["Refinement iterations", "改良の反復回数"],
    "opt.vision":       ["Vision Judge", "画像による検証"],
    "opt.grounding":    ["Standard dimensions", "標準寸法の参照"],
    "opt.grounding.tip": [
      "Give the Planner the published dimensions for any standard part the "
      + "request names — thread sizes, bearings, NEMA frames. Off reproduces "
      + "the pipeline as published.",
      "リクエストに含まれる標準部品（ねじ、軸受、NEMA フレームなど）の"
      + "規格寸法をプランナーに渡します。オフにすると、公開されている"
      + "パイプラインそのままの動作になります。"],
    "opt.provider":     ["Provider", "プロバイダー"],
    "opt.genmodel":     ["Generation model", "生成モデル"],
    "opt.judgemodel":   ["Judge model", "検証モデル"],

    "ph.modelid":       ["model id", "モデル ID"],
    "ph.modelid.count": ["model id ({n} available)", "モデル ID（{n} 件）"],
    "ph.baseurl":       ["Base URL", "ベース URL"],
    "ph.awsregion":     ["AWS region, e.g. us-east-1",
                         "AWS リージョン（例: us-east-1）"],
    "ph.awsprofile":    ["AWS profile (optional)", "AWS プロファイル（任意）"],
    "ph.apikey":        ["API key", "API キー"],
    "ph.apikey.memory": ["API key (memory only)", "API キー（メモリ上のみ）"],
    "ph.apikey.none":   ["API key (not required)", "API キー（不要）"],
    "btn.usekey":       ["Use", "適用"],

    "banner.catalog": [
      "No model backend configured, so the five agents cannot run. Standard "
      + "parts still work — ask for a fastener, bearing, gear, pulley or "
      + "spring and it comes from the catalogue, exactly and instantly. "
      + "Anything custom needs a provider below.",
      "モデルのバックエンドが設定されていないため、5 つのエージェントは"
      + "動作しません。標準部品は利用できます。締結部品・軸受・歯車・"
      + "プーリー・ばねであればカタログから正確に即座に生成されます。"
      + "それ以外の部品には、下でプロバイダーの設定が必要です。"],
    "banner.nobackend": [
      "No model backend configured, so the agents cannot run. Choose a "
      + "provider below, or replay a recorded run.",
      "モデルのバックエンドが設定されていないため、エージェントは"
      + "動作しません。下でプロバイダーを選ぶか、記録済みの実行を"
      + "再生してください。"],

    /* ── viewer ─────────────────────────────────────────────────────── */
    "view.iso":         ["ISO", "等角"],
    "view.front":       ["FRONT", "正面"],
    "view.top":         ["TOP", "上面"],
    "view.right":       ["RIGHT", "右側面"],
    "view.fit":         ["FIT", "全体表示"],
    "view.wire":        ["WIREFRAME", "ワイヤーフレーム"],
    "view.spin":        ["SPIN", "回転"],
    "view.drawing":     ["Drawing", "図面"],

    "empty.title":      ["Nothing built yet", "まだ何も生成されていません"],
    "empty.body": [
      "Describe a part on the left. Five agents plan it, write CadQuery, run "
      + "it through the OpenCASCADE kernel and check the result before "
      + "anything appears here.",
      "左側で部品を説明してください。5 つのエージェントが設計を立て、"
      + "CadQuery を書き、OpenCASCADE カーネルで実行し、結果を検証した"
      + "うえでここに表示されます。"],

    "err.title":        ["The run could not complete", "実行を完了できませんでした"],
    "err.retry":        ["Try again", "再実行"],
    "err.keep":         ["Show best attempt", "最も近い試行を表示"],
    "err.unknown":      ["Unknown error.", "不明なエラーです。"],
    "err.haveattempt":  ["An earlier attempt is still available below.",
                         "下に、これより前の試行が残っています。"],
    "err.checkenv":     ["Check the environment panel in the header, then try again.",
                         "ヘッダーの実行環境パネルを確認してから、再実行してください。"],

    /* ── stages ─────────────────────────────────────────────────────── */
    "stage.plan":       ["Planning the part", "部品を計画中"],
    "stage.code":       ["Writing CadQuery", "CadQuery を記述中"],
    "stage.execute":    ["Building the solid", "ソリッドを構築中"],
    "stage.judge":      ["Validating geometry", "形状を検証中"],
    "stage.done":       ["Ready", "完了"],

    "detail.sending":   ["Sending the request", "リクエストを送信中"],
    "detail.decompose": ["Decomposing the request", "要求を分解中"],
    "detail.apidocs":   ["Retrieving CadQuery API docs", "CadQuery API 文書を参照中"],
    "detail.lines":     ["{n} lines written", "{n} 行を生成"],
    "detail.kernel":    ["Running in the OCCT kernel", "OCCT カーネルで実行中"],
    "detail.execfail":  ["Execution failed — repairing", "実行に失敗 — 修復中"],
    "detail.errorfix":  ["Error Refiner is fixing the script",
                         "エラーリファイナーがスクリプトを修正中"],
    "detail.render":    ["Rendering three views", "三面図をレンダリング中"],
    "detail.judging":   ["The Judge is inspecting the part", "判定モデルが部品を検査中"],
    "detail.refining":  ["Refiner is correcting the geometry",
                         "リファイナーが形状を修正中"],
    "detail.replaying": ["Replaying a recorded run", "記録済みの実行を再生中"],
    "detail.reattach":  ["Reattaching to a run already in progress",
                         "実行中のジョブに再接続中"],
    "detail.grounded":  ["Grounded in {subjects}", "{subjects} の規格寸法を参照"],
    "detail.fromcatalog": ["{title} — from the catalogue", "{title} — カタログより"],

    "log.grounding":    ["Standard dimensions: {message}", "標準寸法: {message}"],

    /* ── agents, as named in the token strip ────────────────────────── */
    "agent.planner":    ["planner", "プランナー"],
    "agent.coder":      ["coder", "コーダー"],
    "agent.errorfix":   ["error refiner", "エラーリファイナー"],
    "agent.judge":      ["judge", "検証"],
    "agent.refiner":    ["refiner", "リファイナー"],

    /* ── token accounting ───────────────────────────────────────────── */
    "usage.free": [
      "No model call — this part came from the catalogue, so it cost no tokens.",
      "モデル呼び出しなし — この部品はカタログから生成されたため、"
      + "トークンを消費していません。"],
    "usage.total":      ["{total} tokens · {in} in · {out} out · {calls}",
                         "{total} トークン · 入力 {in} · 出力 {out} · {calls}"],
    "usage.calls":      ["{n} call", "{n} 回の呼び出し"],
    "usage.calls.pl":   ["{n} calls", "{n} 回の呼び出し"],
    "usage.budget":     ["{pct}% of the {cap} budget", "上限 {cap} の {pct}%"],

    /* ── plan panel ─────────────────────────────────────────────────── */
    "plan.none":        ["No part planned yet.", "まだ部品が計画されていません。"],
    "plan.planning":    ["Planning…", "計画中…"],
    "plan.catalog": [
      "No design plan — {title} is defined by {standard}, so nothing had to "
      + "be worked out.",
      "設計プランはありません。{title} は {standard} で寸法が定められて"
      + "いるため、検討の必要がありません。"],
    "plan.dimensions":  ["TARGET DIMENSIONS", "目標寸法"],
    "plan.constraints": ["CONSTRAINTS", "制約"],
    "plan.bbox":        ["overall bbox", "外形寸法"],

    /* ── code panel ─────────────────────────────────────────────────── */
    "code.heading":     ["Generated CadQuery", "生成された CadQuery"],
    "code.stat":        ["{n} LINES · PYTHON", "{n} 行 · PYTHON"],
    "code.empty":       ["— — —", "— — —"],
    "code.copy":        ["Copy", "コピー"],
    "code.copied":      ["CadQuery source copied", "CadQuery のソースをコピーしました"],

    /* ── kernel facts ───────────────────────────────────────────────── */
    "facts.updated":    ["Model updated", "モデルを更新しました"],
    "facts.standard":   ["Standard part", "標準部品"],
    "facts.validated":  ["Validated", "検証済み"],
    "facts.unvalidated": ["Attempt not yet validated", "この試行は未検証です"],
    "facts.wrong":      ["Measured wrong: {label} {actual}, wanted {expected}",
                         "測定値が不一致: {label} は {actual}（期待値 {expected}）"],
    "facts.bbox":       ["bbox mm", "外形 mm"],
    "facts.volume":     ["volume mm³", "体積 mm³"],
    "facts.faces":      ["faces", "面"],
    "facts.edges":      ["edges", "稜線"],
    "facts.solid":      ["solid", "ソリッド"],
    "facts.watertight": ["WATERTIGHT", "閉じている"],
    "facts.invalid":    ["INVALID", "不正"],

    /* ── the measured checks ────────────────────────────────────────── */
    "spec.heading":     ["MEASURED BY THE KERNEL", "カーネルによる実測"],
    "spec.wanted":      ["wanted {expected}", "期待値 {expected}"],
    "spec.solid_valid": ["watertight solid", "閉じたソリッド"],
    "spec.bbox":        ["overall size (advisory)", "外形寸法（参考）"],
    "spec.num_holes":   ["hole count", "穴の数"],
    "spec.num_holes.advisory": ["hole count (advisory)", "穴の数（参考）"],
    "spec.hole_diameter": ["hole diameter", "穴径"],
    "spec.volume_estimate": ["volume (advisory)", "体積（参考）"],

    /* ── validation panel ───────────────────────────────────────────── */
    "val.heading":      ["Validation", "検証"],
    "val.none":         ["Nothing validated yet.", "まだ検証されていません。"],
    "val.waiting":      ["Waiting for the first attempt…", "最初の試行を待機中…"],
    "val.refused":      ["Refused on measurement", "実測により却下"],
    "val.refused.judgepassed": ["The Judge accepted this, but the kernel disagrees: ",
                                "判定モデルは合格としましたが、カーネルの実測は"
                                + "一致しません: "],
    "val.refused.measured": ["The kernel measured this part against the plan: ",
                             "カーネルが設計プランと照合して実測しました: "],
    "val.refused.tail": [
      ". A measurement settles a dimension; an opinion about one does not.",
      "。寸法は実測が決めるものであり、意見が決めるものではありません。"],
    "val.refused.item": ["{label} measured {actual}, wanted {expected}",
                         "{label} の実測値は {actual}（期待値 {expected}）"],
    "val.refused.judgetoo": [" The Judge also rejected it: {feedback}",
                             " 判定モデルも不合格としています: {feedback}"],
    "val.accepted":     ["Accepted by the Judge", "判定モデルが合格としました"],
    "val.rejected":     ["Rejected by the Judge", "判定モデルが不合格としました"],
    "val.catalog.heading": ["Standard part, served from the catalogue",
                            "カタログから提供された標準部品"],
    "val.catalog.body": [
      "{title} is defined by {standard}, so its dimensions are exact rather "
      + "than estimated. No model wrote it and no Judge assessed it. "
      + "OpenCASCADE built it and reports a valid watertight solid, and the "
      + "source is parametric — edit it like any other part.",
      "{title} は {standard} で定められているため、寸法は推定ではなく"
      + "正確な値です。モデルが記述したものでも、判定モデルが評価したもの"
      + "でもありません。OpenCASCADE が構築し、閉じた正しいソリッドである"
      + "ことを確認しています。ソースはパラメトリックなので、他の部品と"
      + "同じように編集できます。"],
    "val.catalog.thispart": ["This part", "この部品"],
    "val.catalog.itsstandard": ["its standard", "その規格"],
    "val.rebuilt":      ["Rebuilt and checked by the kernel",
                         "カーネルが再構築して検査しました"],
    "val.rebuilt.failed": ["The kernel rejected this solid",
                           "カーネルがこのソリッドを却下しました"],
    "val.rebuilt.body": [
      "OpenCASCADE rebuilt the solid and reports it valid and watertight. "
      + "The vision Judge was not re-run: a parameter patch changes a value "
      + "the script already declares, not the design.",
      "OpenCASCADE がソリッドを再構築し、閉じた正しい形状であることを"
      + "確認しました。画像による検証は再実行していません。パラメータの"
      + "変更は、スクリプトが既に宣言している値を変えるだけで、設計そのもの"
      + "を変えるものではないためです。"],
    "val.rebuilt.body.failed": ["The rebuilt solid failed the kernel's checks.",
                                "再構築したソリッドはカーネルの検査に"
                                + "合格しませんでした。"],
    "val.src.judge":    ["JUDGE MODEL", "検証モデル"],
    "val.src.render":   ["KERNEL METRICS + THREE-VIEW RENDER",
                         "カーネル実測 + 三面レンダリング"],
    "val.src.metrics":  ["KERNEL METRICS ONLY", "カーネル実測のみ"],
    "val.src.catalog":  ["CATALOGUE · {backend} · NO MODEL CALL",
                         "カタログ · {backend} · モデル呼び出しなし"],
    "val.src.kernel":   ["OCCT KERNEL · JUDGE NOT RE-RUN",
                         "OCCT カーネル · 検証は再実行せず"],
    "val.sawheading":   ["WHAT THE JUDGE SAW", "判定モデルが見た画像"],
    "val.sawcaption":   ["ISOMETRIC · HIGH-ANGLE REAR · FRONT PROFILE",
                         "等角 · 斜め後方 · 正面"],
    "val.renderalt":    ["Three-view render", "三面レンダリング"],

    /* ── labels beside the panels ───────────────────────────────────── */
    "label.planner":    ["PLANNER", "プランナー"],
    "label.judge":      ["JUDGE", "検証"],
    "label.planner.model": ["PLANNER · {model}", "プランナー · {model}"],
    "label.judge.model": ["JUDGE · {model}", "検証 · {model}"],
    "label.catalog":    ["CATALOGUE · {backend}", "カタログ · {backend}"],

    /* ── iterations ─────────────────────────────────────────────────── */
    "iter.edit":        ["EDIT {n}", "編集 {n}"],
    "iter.catalog":     ["CATALOG", "カタログ"],
    "iter.iteration":   ["ITER {n}", "反復 {n}"],
    "iter.compare":     ["← {n} attempts · click to compare",
                         "← {n} 件の試行 · クリックで比較"],

    /* ── run lifecycle ──────────────────────────────────────────────── */
    "run.needprompt":   ["Describe the part first.", "先に部品を説明してください。"],
    "run.nogeometry":   ["The pipeline produced no usable geometry.",
                         "パイプラインは利用できる形状を生成しませんでした。"],
    "run.lostconn":     ["Lost the connection to the run.", "実行との接続が切れました。"],
    "run.converged":    ["Converged after {n} iteration{s}{seconds}{cost}",
                         "{n} 回の反復で収束しました{seconds}{cost}"],
    "run.notconverged": [
      "Stopped after {n} iterations without the Judge accepting it — showing "
      + "the closest attempt.",
      "判定モデルの合格を得られないまま {n} 回で終了しました。"
      + "最も近い試行を表示しています。"],
    "run.catalogdone":  ["{title} — from the catalogue{seconds}, no model call",
                         "{title} — カタログより{seconds}、モデル呼び出しなし"],
    "run.standardpart": ["Standard part", "標準部品"],
    "run.seconds":      [" in {s}s", "（{s} 秒）"],
    "run.tokens":       [" · {n} tokens", " · {n} トークン"],
    "run.busy":         ["Something is already running.", "すでに実行中です。"],
    "run.finishedaway": ["That run finished while the page was away",
                         "ページを離れている間に実行が完了しました"],
    "run.reattached":   ["Reattached to a run still in progress",
                         "実行中のジョブに再接続しました"],

    /* ── history ────────────────────────────────────────────────────── */
    "hist.heading":     ["Run History", "実行履歴"],
    "hist.empty":       ["No runs yet.", "まだ実行はありません。"],
    "hist.failed":      ["FAILED", "失敗"],
    "hist.converged":   ["CONVERGED", "収束"],
    "hist.notconverged": ["NOT CONVERGED", "未収束"],
    "hist.replay":      ["REPLAY", "再生"],
    "hist.catalog":     ["CATALOG", "カタログ"],
    "hist.fixture":     ["FIXTURE", "固定応答"],
    "hist.versions":    ["{n} VER", "{n} 版"],
    "hist.replaytip":   ["Replay this run", "この実行を再生"],
    "hist.loaded.converged": ["Loaded a converged run", "収束した実行を読み込みました"],
    "hist.loaded.unconverged": ["Loaded an unconverged run",
                                "未収束の実行を読み込みました"],
    "hist.nogeometry":  ["That run produced no geometry",
                         "この実行は形状を生成しませんでした"],
    "hist.stoppedearly": ["The pipeline stopped before exporting a solid.",
                          "ソリッドを書き出す前にパイプラインが停止しました。"],
    "hist.replaypill":  ["REPLAY · recorded run", "再生 · 記録済みの実行"],

    /* ── provider notes ─────────────────────────────────────────────── */
    /* Sits inside a narrow <select>, so the Japanese is kept short: the
       option truncates before the suffix would otherwise be readable. */
    "prov.needssetup":  [" — needs setup", "（要設定）"],
    "prov.bothroles":   ["Choose a model for both roles.",
                         "両方の役割にモデルを指定してください。"],
    "prov.samemodel": [
      "Both roles use the same model, so the Judge grades its own work. "
      + "Pick a stronger judge model for an independent check.",
      "生成と検証に同じモデルが指定されています。この場合、モデルが自分の"
      + "出力を自分で採点することになります。独立した検証のため、より強い"
      + "モデルを検証側に指定してください。"],
    "prov.local":       ["Running locally — nothing leaves this machine.",
                         "ローカル実行です。この端末の外にデータは出ません。"],
    "prov.ready":       ["Ready.", "準備完了。"],
    "prov.bedrock":     ["Ready — {region}{profile}{credentials}",
                         "準備完了 — {region}{profile}{credentials}"],
    "prov.bedrock.profile": [", profile {profile}", "、プロファイル {profile}"],
    "prov.bedrock.creds": [", credentials from {source}.",
                           "、認証情報の取得元は {source}。"],
    "prov.isready":     ["{label} is ready", "{label} の準備ができました"],
    /* What a provider that is not ready needs. The server sends the same
       sentence in English as a fallback; these are looked up by the key it
       sends alongside it. Environment-variable names are not translated -
       they are typed, not read. */
    "prov.hint.anthropic": ["Set ANTHROPIC_API_KEY in .env",
                            ".env に ANTHROPIC_API_KEY を設定してください"],
    "prov.hint.openai": ["Set OPENAI_API_KEY in .env",
                         ".env に OPENAI_API_KEY を設定してください"],
    "prov.hint.bedrock": [
      "Set AWS_REGION and your usual AWS credentials (AWS_PROFILE, env vars, "
      + "SSO or an instance role)",
      "AWS_REGION と、普段お使いの AWS 認証情報（AWS_PROFILE、環境変数、"
      + "SSO、インスタンスロールのいずれか）を設定してください"],
    "prov.hint.bedrock.noregion": ["Bedrock needs an AWS region",
                                   "Bedrock には AWS リージョンの指定が必要です"],
    "prov.hint.bedrock.nocreds": [
      "AWS credentials not found ({source}). Sign in with `aws sso login`, or "
      + "set AWS_PROFILE.",
      "AWS の認証情報が見つかりません（{source}）。`aws sso login` で"
      + "サインインするか、AWS_PROFILE を設定してください。"],
    "prov.hint.ollama": ["Start Ollama and pull a model, e.g. "
                         + "`ollama pull llama3.1`",
                         "Ollama を起動し、モデルを取得してください"
                         + "（例: `ollama pull llama3.1`）"],
    "prov.hint.lmstudio": ["Start LM Studio's local server",
                           "LM Studio のローカルサーバーを起動してください"],
    "prov.hint.custom": [
      "Set CADSMITH_LLM_BASE_URL (and CADSMITH_LLM_API_KEY if needed) for "
      + "vLLM, llama.cpp, Together, Groq, OpenRouter, and so on",
      "vLLM、llama.cpp、Together、Groq、OpenRouter などを使う場合は "
      + "CADSMITH_LLM_BASE_URL（必要に応じて CADSMITH_LLM_API_KEY）を"
      + "設定してください"],
    "prov.hint.unreachable": ["Nothing is listening at {url}.",
                              "{url} で待ち受けているサーバーがありません。"],
    /* The environment panel. The row names are interface text; the details
       beside them quote the environment - version strings, package names,
       a certificate path - and are left as the environment reports them.
       The one exception is the backend row, which is a sentence to act on. */
    "diag.cadquery":     ["cadquery", "CadQuery"],
    "diag.vision_render": ["vision render", "画像レンダリング"],
    "diag.model_backend": ["model backend", "モデルのバックエンド"],
    "diag.metrics":      ["metrics", "計測ライブラリ"],
    "diag.tls_trust":    ["tls trust", "TLS 証明書"],
    "diag.catalog":      ["catalog", "カタログ"],
    "banner.backend": ["No model backend configured - set a provider key in "
                       + ".env or paste one in the app. Recorded runs still "
                       + "replay, and parameter edits still rebuild.",
                       "モデルのバックエンドが設定されていません。.env に"
                       + "プロバイダーのキーを設定するか、アプリに貼り付けて"
                       + "ください。記録済みの実行は再生でき、パラメータの"
                       + "変更による再構築も可能です。"],
    "prov.stillneeds":  ["{label} still needs setup", "{label} はまだ設定が必要です"],

    /* ── natural-language edits ─────────────────────────────────────── */
    "edit.heading":     ["Natural-language editor", "自然言語エディター"],
    "edit.sub":         ["Edits the current model", "表示中のモデルを編集します"],
    "edit.placeholder": ["Ask for a change…  e.g. make it 15 mm thick, or add "
                         + "a reinforcing gusset",
                         "変更を入力…  例: 厚さを 15 mm にする、補強リブを追加する"],
    "edit.apply":       ["Apply change", "変更を適用"],
    "edit.step.read":   ["Reading the request", "要求を解釈中"],
    "edit.step.apply":  ["Applying the change", "変更を適用中"],
    "edit.step.rebuild": ["Rebuilding in the kernel", "カーネルで再構築中"],
    "edit.step.validate": ["Validating", "検証中"],
    "edit.step.done":   ["Updated", "更新完了"],
    "edit.refineragent": ["REFINER AGENT", "リファイナーエージェント"],
    "edit.needinstruction": ["Describe the change first.",
                             "先に変更内容を入力してください。"],
    "edit.failed":      ["The edit could not be applied.", "変更を適用できませんでした。"],
    "edit.method.patch": ["parameter patch, rebuilt by the kernel",
                          "パラメータ変更、カーネルで再構築"],
    "edit.method.agent": ["Refiner agent", "リファイナーエージェント"],
    "edit.done":        ["Model updated · {method}{seconds}",
                         "モデルを更新しました · {method}{seconds}"],

    /* ── drawing sheet ──────────────────────────────────────────────── */
    "draw.heading":     ["ENGINEERING DRAWING", "製図"],
    "draw.exportpng":   ["Export PNG", "PNG で保存"],
    "draw.back3d":      ["Back to 3D", "3D に戻る"],
    "draw.projecting":  ["Projecting the solid…", "ソリッドを投影中…"],
    "draw.failed":      ["Could not build the drawing.", "図面を作成できませんでした。"],
    "draw.needpart":    ["Generate a part first.", "先に部品を生成してください。"],
    "draw.needdrawing": ["Open a drawing first.", "先に図面を開いてください。"],
    "draw.exported":    ["Drawing exported as PNG", "図面を PNG で保存しました"],
    "draw.rasterfail":  ["Could not rasterise the drawing.",
                         "図面をラスタライズできませんでした。"],

    "plan.heading":     ["Design Plan", "設計プラン"],
    "tier.demo":        ["DEMO", "デモ"],

    /* ── the starting prompts ───────────────────────────────────────────
       These are what actually gets sent, not a caption over an English
       prompt: a Japanese user who presses one should be able to read the
       request the pipeline receives.  The five tiered entries are the
       benchmark prompts from data/dataset_v2 translated term for term -
       every dimension, axis and Z coordinate is carried across unchanged,
       so the part built from either language is the same part. */
    "sample.T1_012": [
      "A triangular prism with an equilateral triangle cross-section having "
      + "a circumscribed circle diameter of 30mm (centroid at the origin), "
      + "extruded to 50mm.",
      "外接円直径 30mm の正三角形断面（重心を原点に置く）を 50mm 押し出した"
      + "三角柱。"],
    "sample.T2_001": [
      "A flat washer: outer diameter 20mm, inner diameter 10.5mm, thickness "
      + "2mm. The washer lies flat on the XY plane, centered at the origin, "
      + "with thickness extruded in the +Z direction.",
      "平座金: 外径 20mm、内径 10.5mm、厚さ 2mm。XY 平面上に原点を中心として"
      + "平らに置き、厚さは +Z 方向に押し出す。"],
    "sample.T2_009": [
      "A flanged bushing standing upright along Z, centered at the origin in "
      + "XY. The flange is at the bottom: 30mm outer diameter, 3mm thick "
      + "(Z=0 to Z=3). The cylindrical body extends upward from the flange: "
      + "20mm outer diameter, 30mm long (Z=3 to Z=33). A 10mm diameter bore "
      + "runs through the entire length of the part.",
      "Z 軸方向に立てた、XY 平面で原点を中心とするつば付きブッシュ。つばは"
      + "下側にあり、外径 30mm、厚さ 3mm（Z=0 から Z=3）。円筒部はつばから"
      + "上に伸び、外径 20mm、長さ 30mm（Z=3 から Z=33）。直径 10mm の穴が"
      + "部品全体を貫通する。"],
    "sample.T3_001": [
      "An open-top rectangular container with rounded vertical corners. The "
      + "outer dimensions are 60mm (X) by 40mm (Y) by 30mm tall (Z). The four "
      + "vertical edges are filleted with a 5mm radius, giving the container "
      + "a smooth rounded-rectangle cross-section. The top face (+Z) is open "
      + "(removed), and the container is shelled inward with 2mm wall "
      + "thickness, preserving the outer dimensions. The container sits on "
      + "the XY plane with its base at Z=0, centered at the origin.",
      "垂直な角を丸めた上面開放の角形容器。外形寸法は X 方向 60mm、Y 方向 "
      + "40mm、高さ Z 方向 30mm。4 本の垂直稜線を半径 5mm でフィレットし、"
      + "断面を滑らかな角丸長方形にする。上面（+Z）は開放（除去）し、外形"
      + "寸法を保ったまま内側へ肉厚 2mm でシェル化する。容器は XY 平面上に"
      + "底面 Z=0 で置き、原点を中心とする。"],
    "sample.T3_007": [
      "A rectangular-to-round duct transition adapter, standing upright "
      + "along the Z axis, centered at the origin in XY. The part has three "
      + "sections from bottom to top: (1) a rectangular mounting flange 70mm "
      + "(X) by 50mm (Y), 3mm thick, from Z=0 to Z=3; (2) a smooth lofted "
      + "transition from a 60mm x 40mm rectangle at Z=3 to a circle of 30mm "
      + "diameter at Z=53; (3) a cylindrical neck 30mm in diameter extending "
      + "from Z=53 to Z=63. All sections are centered at the origin in XY.",
      "Z 軸方向に立てた、XY 平面で原点を中心とする角形−丸形の変換ダクト"
      + "アダプタ。下から順に 3 つの部分からなる。(1) 70mm（X）× 50mm（Y）、"
      + "厚さ 3mm の角形取付フランジ（Z=0 から Z=3）。(2) Z=3 における "
      + "60mm × 40mm の長方形から、Z=53 における直径 30mm の円へ滑らかに"
      + "ロフトした遷移部。(3) Z=53 から Z=63 まで伸びる直径 30mm の円筒"
      + "ネック。いずれの部分も XY 平面で原点を中心とする。"],
    "sample.demo_bracket": [
      "A mounting bracket with a 100mm x 60mm base plate 10mm thick, two "
      + "vertical support walls 45mm tall at each end, and four 8mm mounting "
      + "holes in a rectangular pattern on the base.",
      "100mm × 60mm、厚さ 10mm のベースプレートに、両端へ高さ 45mm の垂直な"
      + "支持壁を 2 枚立て、ベースに直径 8mm の取付穴を長方形配置で 4 つ"
      + "設けた取付ブラケット。"],
    "sample.demo_flange": [
      "A pipe flange with 100mm outer diameter, 40mm central bore, 12mm "
      + "thick, with six 10mm bolt holes evenly spaced on a 78mm bolt circle.",
      "外径 100mm、中心穴径 40mm、厚さ 12mm の配管フランジ。直径 78mm の"
      + "ボルト円上に直径 10mm のボルト穴を 6 つ等間隔に配置する。"],
  };

  const INDEX = { en: 0, ja: 1 };

  function stored() {
    try { return localStorage.getItem(STORE_KEY); } catch (e) { return null; }
  }

  /* The browser's own preference decides the first visit, so a Japanese
     machine opens in Japanese without anyone being told there is a switch.
     An explicit choice, once made, outranks it. */
  function initial() {
    const saved = stored();
    if (saved && INDEX[saved] !== undefined) return saved;
    const wanted = (navigator.languages || [navigator.language || "en"]);
    for (const tag of wanted) {
      const code = String(tag).toLowerCase().split("-")[0];
      if (INDEX[code] !== undefined) return code;
    }
    return "en";
  }

  let lang = initial();
  const listeners = [];

  /* Missing keys fall back to English rather than showing the key: a gap in
     the dictionary should read as untranslated, not as broken. */
  function t(key, params) {
    const entry = DICT[key];
    let text = entry ? (entry[INDEX[lang]] || entry[0]) : key;
    if (params) {
      text = text.replace(/\{(\w+)\}/g, (whole, name) =>
        params[name] !== undefined ? String(params[name]) : whole);
    }
    return text;
  }

  /* Plural is an English problem: Japanese has no plural agreement, so the
     two forms resolve to the same string there and nothing looks odd. */
  function plural(singularKey, pluralKey, n, params) {
    return t(n === 1 ? singularKey : pluralKey,
             Object.assign({ n: n }, params || {}));
  }

  function apply(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach(el => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    scope.querySelectorAll("[data-i18n-ph]").forEach(el => {
      el.placeholder = t(el.getAttribute("data-i18n-ph"));
    });
    scope.querySelectorAll("[data-i18n-title]").forEach(el => {
      el.title = t(el.getAttribute("data-i18n-title"));
    });
    scope.querySelectorAll("[data-i18n-alt]").forEach(el => {
      el.alt = t(el.getAttribute("data-i18n-alt"));
    });
    if (!root) {
      document.documentElement.lang = lang;
      document.title = t("app.title");
    }
  }

  function set(next) {
    if (INDEX[next] === undefined || next === lang) return;
    lang = next;
    try { localStorage.setItem(STORE_KEY, lang); } catch (e) { /* fine */ }
    apply();
    listeners.forEach(fn => { try { fn(lang); } catch (e) { /* keep going */ } });
  }

  return {
    LANGS: LANGS,
    t: t,
    plural: plural,
    apply: apply,
    set: set,
    has: key => DICT[key] !== undefined,
    get current() { return lang; },
    onChange: fn => listeners.push(fn),
    /* Exposed for the parity test, which fails the build if a key is
       present in one language and missing in the other. */
    _dict: DICT,
  };
})();

const t = I18N.t;
