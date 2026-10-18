"""Prepare one reviewed source/log snapshot and admit its original replay only."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence_io import local_evidence_path
from .local_execution import (
    STAGES,
    IndependentOracle,
    LocalCheckConfig,
    LocalCommand,
    _checked,
)
from .task import (
    CaseSelection,
    digest,
    prepare_task,
    require_relative_reference,
    verify_task,
)
from .verification import _atomic_json

MAX_CONFIG_BYTES = 1048576
MAX_OUTPUT_BYTES = 65536
SOURCE = "commons/lib_foundation/src/main/ets/http/OrderRequestChecksum.ets"
ORACLE = "resources/source-validation/household-checksum/oracle.cjs"
MARKER = "DFX022_TARGET_RANGE_ERROR"
SCOPE = (
    "Execute actual OrderRequestChecksum.create/verify after Node stripTypeScriptTypes, "
    "erased IRequest interface import and local export binding; fixed ASCII/CJK/emoji "
    "requests, tampering and repeated/frozen inputs; no PRE_ORDER, UI, device or server coverage"
)
REVIEWED_ORACLES = {
    ORACLE: (SOURCE, MARKER, SCOPE),
    "resources/source-validation/tourist-phone/oracle.cjs": (
        "components/module_base/src/main/ets/util/CommonUtil.ets",
        "DFX016_TARGET_REGEX_SYNTAX_ERROR",
        ("Execute actual module_base CommonUtil after Node stripTypeScriptTypes; "
         "fixed phone validation, padWithZeros and CardNo cases; no UI or caller-substitution coverage"),
    ),
    "resources/source-validation/office-tasks/oracle.cjs": (
        "scenes/agency/src/main/ets/mock/DataManager.ets",
        "DFX017_TARGET_REGEX_SYNTAX_ERROR",
        ("Execute actual DataManager Map/saveTodos/loadTodosByState/queryTasks after Node type erasure; "
         "unused preferences and TaskItem type imports removed; fixed state/literal-search and stock checks; "
         "no persistence, UI, device or caller-substitution coverage"),
    ),
    "resources/source-validation/enterprise-property/oracle.cjs": (
        "commons/component/src/main/ets/utils/PropUtil.js",
        "DFX009_TARGET_IN_NON_OBJECT",
        ("Execute the actual hasKey JavaScript module with local export rebinding; "
         "undefined/null targets, own/inherited/callable/dictionary property checks and input immutability; "
         "no ArkUI events, resume persistence or device coverage"),
    ),
    "resources/source-validation/resource-sort/oracle.cjs": (
        "components/money_track/src/main/ets/viewmodels/ResourceManageVM.ets",
        "DFX001_TARGET_SORTNUM_UNDEFINED",
        ("Execute actual complete customSortResource method after unique-header/class-end extraction and Node type erasure; "
         "BalanceChangeType strings and resource arrays supplied; both categories, integer bounds, swaps and slot sortNum; "
         "no Grid routing, persistence or resource-initialization coverage"),
    ),
    "resources/source-validation/teleprompter-font/oracle.cjs": (
        "components/teleprompter/src/main/ets/components/ConfigSheet.ets",
        "DFX026_TARGET_TOFIXED_RANGE",
        ("Execute actual complete font-size changeValue callback with Node type erasure; "
         "default interval read from checked PrompterConfigModel, normal/endpoints/repeated updates and unrelated state; "
         "no ArkUI observation, rendering, event delivery or device coverage"),
    ),
    "resources/source-validation/music-slider/oracle.cjs": (
        "components/module_musicplay/src/main/ets/components/PlayerControlArea.ets",
        "DFX025_TARGET_REPEAT_RANGE",
        ("Execute actual complete progress-slider onChange callback with Node type erasure; "
         "finite marker calculations, Begin/End seek, paused/moving events and zero/one/normal duration; "
         "no audio output, player timing, UI rendering or device coverage"),
    ),
    "resources/source-validation/calendar-days/oracle.cjs": (
        "components/date_calculation/src/main/ets/utils/Utils.ets",
        "DFX007_TARGET_BIGINT_MATH_FLOOR",
        ("Execute actual complete calculateDaysBetweenDates after newline normalization and Node type erasure; "
         "representative BigInt fault reproducer, absolute whole-day intervals, UTC leap/year/fractional boundaries; "
         "no lunar conversion, date-picker or UI coverage"),
    ),
    "resources/source-validation/asset-pie/oracle.cjs": (
        "features/assets/src/main/ets/viewmodels/AssetAnalysisVM.ets",
        "DFX005_TARGET_OBJECT_KEYS_UNDEFINED",
        ("Execute actual complete pieChartData getter after newline normalization and Node type erasure; "
         "empty groups, fund/credit projections, sign/order/resource and state preservation; "
         "no DB loading, observed scheduling, chart UI or device coverage"),
    ),
    "resources/source-validation/accounting-formatter/oracle.cjs": (
        "components/bill_chart/src/main/ets/components/BillBarChart.ets",
        "DFX002_TARGET_CONSTRUCTOR_WITHOUT_NEW",
        ("Execute actual factory field and complete MyAxisValueFormatter class with Node type erasure; "
         "construction, distinct instances and dayjs offset/unit/pattern delegation; "
         "no dayjs calendar arithmetic, chart loading, UI or device coverage"),
    ),
    "resources/source-validation/book-lifecycle/oracle.cjs": (
        "feature/book_home/src/main/ets/views/BookViewListPage.ets",
        "DFX004_TARGET_UNINITIALIZED_LOAD",
        ("Execute complete actual aboutToAppear and actual LazyDataSource/LazyDataVM with Node type erasure; "
         "initialization, route/category, populated/empty/missing parameters and reentry; "
         "no ArkUI observation, navigation, UI or device coverage"),
    ),
    "resources/source-validation/express-template/oracle.cjs": (
        "features/business_home/src/main/ets/viewModels/ExpressPageVM.ets",
        "DFX010_TARGET_ARRAYLIST_INDEX",
        ("Execute complete actual changeTemplate method with Node type erasure; "
         "empty/partial slots, hour cutoffs, address/goods/payment branches and async geocoding delegation; "
         "declared ArrayList subset and service recorders; no actual map, pricing, network or UI"),
    ),
    "resources/source-validation/notes-serialization/oracle.cjs": (
        "common/datasource/src/main/ets/NoteService.ets",
        "DFX006_TARGET_CIRCULAR_JSON",
        ("Execute complete actual add/update/list/search/recent methods and persistence helper with Node type erasure; "
         "cycle-free JSON, identity/order/search/category counts, updates and persistence payloads; "
         "no real rich-text codec, KV/preferences, card UI or device coverage"),
    ),
    "resources/source-validation/pcm-frames/oracle.cjs": (
        "components/audio_common/src/main/ets/viewModel/EditViewModel.ets",
        "DFX021_TARGET_PCM_VIEW_BOUNDS",
        ("Load complete actual audio_common module with metadata/type erasure; exercise initProps and parsePcmFile; "
         "complete mono/stereo f32le sampled frames, partial tails, empty data and file-close delegation; "
         "supplied fileIo bytes, no actual audio, worker, canvas, SDK or UI"),
    ),
    "resources/source-validation/learning-order/oracle.cjs": (
        "components/select_category/src/main/ets/components/MainPage.ets",
        "DFX019_TARGET_LEARNING_CYCLE",
        ("Load actual page fields/lifecycle/helpers before UI build plus model classes after metadata/type erasure; "
         "learning links/fallback, cycles, missing targets, display copies and repeated initialization; "
         "plain parameters, no ArkUI, navigation or huge-graph scalability coverage"),
    ),
    "resources/source-validation/post-images/oracle.cjs": (
        "components/travel_discovery/src/main/ets/model/Index.ets",
        "DFX012_TARGET_IMAGE_JSON",
        ("Load actual model module after reviewed import/metadata/type erasure; exercise PostCardModel; "
         "raw URI, JSON/array images, empty/invalid input normalization and unrelated fields; "
         "no image decoding, network, listener, ArkUI or device coverage"),
    ),
    "resources/source-validation/share-fingerprint/oracle.cjs": (
        "components/aggregated_share/src/main/ets/views/ShareView.ets",
        "DFX_B2_024_TARGET_SHARE_ALIGNMENT",
        ("Execute complete actual share aboutToAppear after wrapping/type erasure; "
         "unaligned/Unicode UTF-8, deterministic content-sensitive hash and setup delegation; "
         "declared encoder/storage/share recorders, no exact hash algorithm or real transport/UI coverage"),
    ),
    "resources/source-validation/exam-search/oracle.cjs": (
        "commons/datasource/src/main/ets/ExamService.ets", "DFX014_TARGET_SEARCH_REGEX",
        "Actual complete exam search method/helper, literal keywords and selected nonsearch branches; declared enums/questions, no generation, persistence or UI",
    ),
    "resources/source-validation/fitness-search/oracle.cjs": (
        "components/search/src/main/ets/views/ResultPage.ets", "DFX015_TARGET_GROUP_NAME",
        "Actual complete user-search callback, literal address/city filtering and VM/popup delegation; declared storage/stores, no surrounding ArkUI or network",
    ),
    "resources/source-validation/cutlery-count/oracle.cjs": (
        "features/order/src/main/ets/pages/ConfirmOrderPage.ets", "DFX020_TARGET_CUTLERY_ARRAY_LENGTH",
        "Actual complete cutlery-confirm callback, none/by-order/fixed counts and labels/reset/close; plain picker/cart and resource sentinel, no UI/order transport",
    ),
    "resources/source-validation/video-share/oracle.cjs": (
        "commons/utils/src/main/ets/player/VideoPlayPage.ets", "DFX011_TARGET_SHARE_CONSTRUCTION",
        "Actual complete shareEvent field with class wrapper/type erasure; native class-shaped constructor/show recorder and payload preservation; no real sharing SDK, transport or UI",
    ),
    "resources/source-validation/news-author/oracle.cjs": (
        "commons/lib_news_feed_details/src/main/ets/views/ArticleFeedDetails.ets", "DFX_NEWS_TARGET_MISSING_AUTHOR",
        "Actual complete readNewsBuilder after bounded inert Row lowering/type erasure; optional author, loading guard and reader payload; declared reader/body/cover recorders, no actual ArkUI or playback",
    ),
    "resources/source-validation/order-logistics/oracle.cjs": (
        "features/order/src/main/ets/views/OrderInfoPage.ets", "DFX_MALL_TARGET_LOGISTICS_UNDEFINED",
        "Actual complete buildAddressCard after bounded container/Row/style lowering/type erasure; missing/normal logistics, address/edit/express and pickup/refund delegation; no ArkUI hierarchy, rendering or asynchronous refresh",
    ),
    "resources/source-validation/calendar-selection/oracle.cjs": (
        "features/statistics/src/main/ets/components/CalendarSummary.ets", "DFX_B2_005_TARGET_EMPTY_DATE",
        "Actual complete date-click callback with lexical this/type erasure; populated/empty/missing date selection and refresh delegation; ISO dayjs recorder, no date algorithm, DB or UI",
    ),
    "resources/source-validation/cache-read/oracle.cjs": (
        "components/feedback/src/main/ets/utils/FileUtils.ets", "DFX_B3_024_TARGET_CACHE_ALIGNMENT",
        "Actual complete verifyCacheFile/safeClose region with class wrapper/type erasure; alignment, read/close and unavailable/denied/read-error branches; declared in-memory fs/context, discarded checksum correctness and real SDK filesystem not certified",
    ),
}
REVIEWED_MOCKS = {
    "resources/source-validation/resource-sort/oracle.cjs": [
        "BalanceChangeType string constants; supplied income/expense resource arrays",
    ],
    "resources/source-validation/teleprompter-font/oracle.cjs": [
        "hilog.error capture; plain config/configList state instead of ArkUI observation",
    ],
    "resources/source-validation/music-slider/oracle.cjs": [
        "SliderChangeMode equality sentinels; Logger.info capture; MediaService.seek recorder; plain musicModel",
    ],
    "resources/source-validation/asset-pie/oracle.cjs": [
        "AssetChangeType string constants; plain fund/credit groups instead of observed DB-backed state",
    ],
    "resources/source-validation/accounting-formatter/oracle.cjs": [
        "dayjs add/format delegation recorder; plain class hosting actual factory field",
    ],
    "resources/source-validation/book-lifecycle/oracle.cjs": [
        "TCRouter parameter provider and route constant sentinel; no ArkUI observation",
    ],
    "resources/source-validation/express-template/oracle.cjs": [
        "ArrayList checked-index/add/length subset; shipping/payment constants; fixed clock hour; geocoding/price recorders",
    ],
    "resources/source-validation/notes-serialization/oracle.cjs": [
        "Plain notes/category state; hilog sink; deferred rich-text codec and KV recorder; UI context/preferences recorder",
    ],
    "resources/source-validation/pcm-frames/oracle.cjs": [
        "fileIo stat/open/read/close over supplied f32le bytes; no actual SDK file IO",
    ],
    "resources/source-validation/learning-order/oracle.cjs": [
        "Plain secondParamObj replacing ArkUI Param delivery; resource-name sentinel; actual model classes retained",
    ],
    "resources/source-validation/post-images/oracle.cjs": [
        "StatusFLAG NO=N/YES=Y constants; observation metadata removed",
    ],
    "resources/source-validation/share-fingerprint/oracle.cjs": [
        "Kit-shaped TextEncoder backed by native UTF-8; AppStorage/share/WX/window-padding recorders",
    ],
    "resources/source-validation/exam-search/oracle.cjs": ["Declared numeric enum values and question records"],
    "resources/source-validation/fitness-search/oracle.cjs": ["Current-city storage, store records, VM submit/close and SearchSource sentinel"],
    "resources/source-validation/cutlery-count/oracle.cjs": ["Plain picker/cart state and resource-name sentinel"],
    "resources/source-validation/video-share/oracle.cjs": ["Native class-shaped SharePicker constructor/show recorder; plain video state"],
    "resources/source-validation/news-author/oracle.cjs": ["Inert Row/TextRead recorders; split/body/cover helpers and plain article state"],
    "resources/source-validation/order-logistics/oracle.cjs": ["Inert UI primitives/styles/events, resource/order-label helper, address/pickup delegates and plain view-model state"],
    "resources/source-validation/calendar-selection/oracle.cjs": ["Already-ISO dayjs format delegation; plain bill list and getSelectedDateData recorder"],
    "resources/source-validation/cache-read/oracle.cjs": ["Context/area values and in-memory fs access/open/stat/read/close recorders"],
}


def _read(path: Path, cap: int) -> bytes:
    source = _checked(path)
    io_source = local_evidence_path(source)
    if not io_source.is_file() or io_source.stat().st_size > cap:
        raise ValueError("preparation input is missing or oversized")
    with io_source.open("rb") as stream:
        content = stream.read(cap + 1)
    if len(content) > cap:
        raise ValueError("preparation input is oversized")
    return content


def _owned(root: Path, value: str, owner: str, *, exists: bool = False) -> Path:
    require_relative_reference(value)
    result = _checked(root / value, exists=exists)
    boundary = _checked(root / owner)
    if result == boundary or not result.is_relative_to(boundary):
        raise ValueError("preparation path is outside its declared owner")
    return result


def _write_new(path: Path, value: dict[str, Any]) -> None:
    _checked(path, exists=False)
    local_evidence_path(path.parent).mkdir(parents=True, exist_ok=True)
    with local_evidence_path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=True, indent=2) + "\n")


def _baseline_stage(node: Path, oracle: Path, project: Path, stage: str) -> dict[str, Any]:
    # This is the fixed host oracle and original reviewed source, not a model
    # command or candidate. It inherits the verified managed Job. NODE_OPTIONS
    # and model credentials are not inherited into the native runtime.
    environment = {name: value for name, value in os.environ.items()
                   if name.casefold() in {"systemroot", "windir", "temp", "tmp", "path"}}
    try:
        completed = subprocess.run([str(node), str(oracle), stage], cwd=project,
            env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=10, check=False, shell=False)
    except subprocess.TimeoutExpired:
        raise ValueError("original replay exceeded its ten-second limit") from None
    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise ValueError("original replay exceeded its output limit")
    return {"stage": stage, "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
        "stdout_sha256": digest(completed.stdout), "stderr_sha256": digest(completed.stderr)}


def _prepare_analysis(root: Path, config: Path, raw: dict[str, Any]) -> dict[str, Any]:
    """Prepare reviewed inputs only; never run a command or invent validation."""
    if set(raw) != {"mode", "case", "data_root", "output", "record"} or raw["data_root"] != "data_B":
        raise ValueError("analysis preparation fields or dataset root differ")
    case = CaseSelection.model_validate(raw["case"])
    if not isinstance(raw["output"], str) or not isinstance(raw["record"], str):
        raise TypeError("analysis preparation paths must be strings")
    data = _checked(root / "data_B")
    output = _owned(root, raw["output"], ".dfx-local/tmp")
    record_path = _owned(root, raw["record"], ".dfx-local/state")
    if (record_path == config or record_path.suffix != ".json" or
            local_evidence_path(output).exists() or local_evidence_path(record_path).exists()):
        raise ValueError("analysis preparation refuses existing or overlapping outputs")
    record: dict[str, Any] = {"schema_version": 1, "state": "pending", "status": "preparing",
        "observed_at": datetime.now(UTC).isoformat(), "case_identity_sha256": case.sha256(),
        "task_id": case.task_id, "task": str(output), "model_calls": 0,
        "source_verification": "not_run", "application_verification": "not_run",
        "candidate_created": False, "repair_success": None}
    _write_new(record_path, record)
    try:
        snapshot = prepare_task(data, case.model_dump(exclude_none=True), output)
        task = verify_task(output)
        record.update(state="done", status="analysis_inputs_prepared",
            source_snapshot_sha256=snapshot["source_snapshot_sha256"], source_files=snapshot["source_files"],
            source_log_alignment=task["source_log_alignment"])
    except Exception as error:
        record.update(state="done", status="blocked", error_type=type(error).__name__, error_detail=str(error)[:1024])
        _atomic_json(record_path, record)
        raise
    _atomic_json(record_path, record)
    return record


def run_preparation(root: Path, config_path: Path | str) -> dict[str, Any]:
    from .managed_runtime import require_managed_worker

    root = _checked(root)
    require_managed_worker(root, "prepare")
    config = Path(config_path)
    config = _checked(config if config.is_absolute() else root / config)
    state = _checked(root / ".dfx-local/state")
    if not config.is_relative_to(state) or config.suffix != ".json":
        raise ValueError("preparation configuration must be a host state JSON")
    raw = json.loads(_read(config, MAX_CONFIG_BYTES).decode("utf-8-sig"))
    if isinstance(raw, dict) and raw.get("mode") == "analysis":
        return _prepare_analysis(root, config, raw)
    expected_fields = {"case", "data_root", "output", "validation_profile", "node", "oracle", "record"}
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ValueError("preparation configuration has unexpected fields")
    case = CaseSelection.model_validate(raw["case"])
    if any(not isinstance(raw[key], str) for key in expected_fields - {"case"}):
        raise ValueError("preparation paths must be strings")
    require_relative_reference(raw["data_root"])
    if raw["data_root"] != "data_B":
        raise ValueError("pilot preparation only admits the reviewed data_B root")
    data = _checked(root / raw["data_root"])
    if not data.is_relative_to(root) or not data.is_dir():
        raise ValueError("dataset must remain inside the workspace")
    output = _owned(root, raw["output"], ".dfx-local/tmp")
    profile_path = _owned(root, raw["validation_profile"], ".dfx-local/state")
    record_path = _owned(root, raw["record"], ".dfx-local/state")
    if (profile_path.suffix != ".json" or record_path.suffix != ".json" or
            len({config, profile_path, record_path}) != 3 or
            any(local_evidence_path(path).exists() for path in (output, profile_path, record_path))):
        raise ValueError("preparation refuses existing or overlapping outputs")
    if output.is_relative_to(data) or data.is_relative_to(output):
        raise ValueError("preparation output overlaps dataset")
    if raw["oracle"] not in REVIEWED_ORACLES:
        raise ValueError("only reviewed source oracles are admitted")
    selected_source, selected_marker, selected_scope = REVIEWED_ORACLES[raw["oracle"]]
    selected_mocks = REVIEWED_MOCKS.get(raw["oracle"], [])
    oracle = _checked(root / raw["oracle"])
    oracle_sha = digest(_read(oracle, MAX_CONFIG_BYTES))
    node = Path(raw["node"])
    if not node.is_absolute():
        raise ValueError("Node must be an absolute native executable")
    node = _checked(node)
    if not node.is_file() or node.suffix.casefold() != ".exe" or ".codex" in {
            part.casefold() for part in node.parts}:
        raise ValueError("Node must be an existing ordinary native executable")
    record: dict[str, Any] = {"schema_version": 1, "state": "pending", "status": "preparing",
        "observed_at": datetime.now(UTC).isoformat(), "case_identity_sha256": case.sha256(),
        "task_id": case.task_id, "task": str(output), "validation_profile": str(profile_path),
        "oracle": str(oracle), "oracle_sha256": oracle_sha, "node": str(node),
        "validation_scope": selected_scope, "mocked_interfaces": selected_mocks, "stages": [],
        "source_verification": "not_run", "application_verification": "not_run",
        "repair_success": None, "candidate_created": False, "model_calls": 0}
    _write_new(record_path, record)
    try:
        snapshot = prepare_task(data, case.model_dump(exclude_none=True), output)
        task = verify_task(output)
        source = output / "source"
        source_sha = digest(_read(source / selected_source, MAX_CONFIG_BYTES))
        record.update(source_snapshot_sha256=snapshot["source_snapshot_sha256"],
            source_files=snapshot["source_files"], original_source_sha256=source_sha,
            source_log_alignment=task["source_log_alignment"])
        for stage in ("build", "target"):
            verify_task(output)
            if digest(_read(oracle, MAX_CONFIG_BYTES)) != oracle_sha:
                raise ValueError("oracle changed before original replay")
            observed = _baseline_stage(node, oracle, source, stage)
            record["stages"].append(observed)
            _atomic_json(record_path, record)
            verify_task(output)
            if digest(_read(oracle, MAX_CONFIG_BYTES)) != oracle_sha:
                raise ValueError("oracle changed during original replay")
            try:
                header = json.loads(observed["stdout"].splitlines()[0])
            except (IndexError, TypeError, json.JSONDecodeError):
                raise ValueError("original replay did not identify its source bytes") from None
            if (not isinstance(header, dict) or header.get("stage") != stage or
                    header.get("source_sha256") != source_sha):
                raise ValueError("original replay source identity differs")
            if stage == "build" and observed["exit_code"] != 0:
                raise ValueError("original source could not be loaded")
            if stage == "target" and (observed["exit_code"] != 9 or
                    selected_marker not in observed["stdout"] + observed["stderr"]):
                raise ValueError("original did not reproduce the reviewed fault")
        profile = LocalCheckConfig(mode="source", project_root=str(source),
            validation_scope=selected_scope, mocked_interfaces=selected_mocks, commands={stage: LocalCommand(
                argv=[str(node), str(oracle), stage], oracle_sha256=oracle_sha, timeout_seconds=10)
                for stage in STAGES}, independent_oracles={stage: IndependentOracle(
                    path=str(oracle), sha256=oracle_sha) for stage in STAGES},
            baseline_target_exit_codes=[9], baseline_fault_pattern=selected_marker, max_check_seconds=90)
        _write_new(profile_path, profile.model_dump(mode="json"))
        record.update(state="done", status="source_baseline_admitted", fault_reproduced=True,
            baseline_verification="reproduced", source_verification="not_run",
            profile_sha256=digest(_read(profile_path, MAX_CONFIG_BYTES)))
    except Exception as error:
        record.update(state="done", status="blocked", fault_reproduced=False,
            error_type=type(error).__name__, error_detail=str(error)[:1024])
        _checked(record_path)
        _atomic_json(record_path, record)
        raise
    _checked(record_path)
    _atomic_json(record_path, record)
    return record
