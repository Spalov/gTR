import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .backup import backup_project
from .conversion import MEDIA_MODES, RECORD_KINDS, load_bundle, plan_import, prepare
from .core import analyze, catalogue, summary, verify
from .faults import ImportFault
from .files import local, publish_text
from .journal import Endpoint, Journal
from .mock_api import SCENARIOS, rehearse
from .model import RecoveryError, quiet
from .reports import plan_summary, redact_verification, save_report
from .verification import compare_files
from .video import VIDEO_POLICIES, MediaTools


class RussianParser(argparse.ArgumentParser):
    def format_help(self):
        return (
            super()
            .format_help()
            .replace("usage: ", "Использование ")
            .replace("positional arguments:", "Позиционные параметры")
            .replace("options:", "Параметры")
            .replace("show this help message and exit", "Показать справку")
            .replace("show program's version number and exit", "Показать версию")
        )

    def error(self, message):
        self.exit(2, "Проверьте параметры команды. Справка доступна через --help.\n")


def arguments():
    parser = RussianParser(prog="gtr", description="Локальная реконструкция переписки Telegram Desktop")
    parser.add_argument("--version", action="version", version="gTR " + __version__)
    commands = parser.add_subparsers(dest="command")
    gui = commands.add_parser("gui", help="Открыть приложение")
    gui.add_argument("--project", type=Path)
    scan = commands.add_parser("analyze", help="Создать локальный проект")
    scan.add_argument("source", type=Path)
    scan.add_argument("--project", type=Path, required=True)
    scan.add_argument("--timezone", default="UTC")
    scan.add_argument("--format", choices=("auto", "html", "json"), default="auto")
    for command in ("report", "verify", "chats"):
        p = commands.add_parser(command)
        p.add_argument("project", type=Path)
        if command == "report":
            p.add_argument("--out", type=Path)
    backup = commands.add_parser("backup", help="Создать проверенную резервную копию проекта")
    backup.add_argument("project", type=Path)
    backup.add_argument("--out", required=True, type=Path)
    for command, title in (
        ("prepare", "Подготовить пакет одного диалога"),
        ("plan", "Оценить будущий пакет"),
    ):
        convert = commands.add_parser(command, help=title)
        convert.add_argument("project", type=Path)
        convert.add_argument("--chat", type=int, required=True)
        convert.add_argument("--out", type=Path, required=command == "prepare")
        convert.add_argument("--timezone", default="source")
        convert.add_argument("--profile", choices=("ios", "android"), default="ios")
        convert.add_argument("--sample", type=int)
        convert.add_argument("--first-record", type=int, default=1, help="Первый исходный номер записи")
        convert.add_argument("--last-record", type=int, help="Последний исходный номер записи включительно")
        convert.add_argument("--record-kind", choices=RECORD_KINDS, default="all", help="Выбор типа исходной записи")
        convert.add_argument("--dates", choices=("historical", "ordered"), default="historical")
        convert.add_argument("--media-mode", choices=MEDIA_MODES, default="text_media", help="Режим вложений")
        convert.add_argument("--video-policy", choices=VIDEO_POLICIES, default="original",
                             help="Как подготовить видео. compatible разрешает перекодирование копии")
        convert.add_argument("--ffmpeg", type=Path)
        convert.add_argument("--ffprobe", type=Path)
        convert.add_argument("--historical-notes", action="store_true",
                             help="Вставить отмеченные исторические подписи и цитаты в текст")
    fixture = commands.add_parser("fixture", help="Создать локальный синтетический пакет")
    series = commands.add_parser(
        "fixture-series", help="Создать три синтетических экспорта для проверки продолжения"
    )
    series.add_argument("--out", type=Path, required=True)
    fixture.add_argument("--out", type=Path, required=True)
    fixture.add_argument("--scenario", choices=("text", "media"), default="text")
    fixture.add_argument(
        "--size", type=int, choices=(2, 10, 100), default=2, help="Число синтетических записей"
    )
    media = commands.add_parser("media-check", help="Проверить типы, хеши и совместимость вложений локально")
    media.add_argument("path", type=Path)
    media.add_argument("--project", action="store_true", help="Проверить проект вместо пакета")
    media.add_argument("--chat", type=int, help="Ограничить проверку проекта одним диалогом")
    media.add_argument("--out", type=Path, help="Сохранить подробную сводку в новый JSON")
    media.add_argument(
        "--private", action="store_true", help="Включить пути внутри архива и контрольные суммы"
    )
    mock = commands.add_parser("simulate", help="Выполнить локальную имитацию")
    mock.add_argument("bundle", type=Path)
    mock.add_argument("--scenario", choices=SCENARIOS, default="success")
    mock.add_argument("--project", type=Path)
    live = commands.add_parser("live", help="Открыть ручной импорт владельца")
    live.add_argument("bundle", type=Path)
    live.add_argument("--peer", required=True)
    live.add_argument("--project", type=Path)
    live.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    observe = commands.add_parser("observe", help="Снимок диапазона диалога для владельца")
    observe.add_argument("bundle", type=Path)
    observe.add_argument("--peer", required=True)
    observe.add_argument("--after-id", default=0, type=int, help="Исключённая нижняя граница ID")
    observe.add_argument("--through-id", type=int)
    observe.add_argument("--out", required=True, type=Path)
    observe.add_argument("--probe-media", action="store_true")
    observe.add_argument("--project", type=Path)
    observe.add_argument("--baseline", type=Path, help="Граница до импорта для этого аккаунта")
    observe.add_argument("--historical-from", help="Историческая дата начала в UTC (ГГГГ-ММ-ДД)")
    observe.add_argument("--historical-through", help="Историческая дата окончания в UTC (ГГГГ-ММ-ДД)")
    observe.add_argument("--max-scan", type=int, help="Максимум прочитанных записей")
    observe.add_argument(
        "--media-hash-limit", default=0, type=int, help="Общий предел полной загрузки медиа в байтах"
    )
    baseline = commands.add_parser("baseline", help="Прочитать последний ID диалога для владельца")
    baseline.add_argument("bundle", type=Path)
    baseline.add_argument("--peer", required=True)
    baseline.add_argument("--out", type=Path, help="Сохранить границу с привязкой к пакету")
    baseline.add_argument("--project", type=Path)
    evidence = commands.add_parser("verify-import", help="Локально сравнить снимки с пакетом")
    evidence.add_argument("bundle", type=Path)
    evidence.add_argument("captures", type=Path, nargs="+")
    evidence.add_argument("--mapping", type=Path)
    evidence.add_argument("--out", type=Path)
    evidence.add_argument("--project", type=Path)
    evidence.add_argument("--private", action="store_true", help="Включить частные данные выбранных снимков")
    fidelity_report = commands.add_parser("fidelity", help="Сверить точность восстановления по сообщениям")
    fidelity_report.add_argument("bundle", type=Path)
    fidelity_report.add_argument("captures", type=Path, nargs="*")
    fidelity_report.add_argument("--mapping", type=Path)
    fidelity_report.add_argument("--project", type=Path)
    fidelity_report.add_argument("--out", type=Path)
    fidelity_report.add_argument("--private", action="store_true", help="Включить локальные даты, ID и имена файлов")
    jobs = commands.add_parser("operations", help="Показать локальный журнал серверных задач")
    jobs.add_argument("--events", metavar="ID")
    diagnostic = commands.add_parser("import-diagnostic", help="Сохранить диагностику по ID операции")
    diagnostic.add_argument("operation_id")
    diagnostic.add_argument("--out", type=Path, required=True)
    diagnostic.add_argument("--private", action="store_true", help="Включить локальные технические поля")
    recover = commands.add_parser("recover-import", help="Снять локальную блокировку прерванной подготовки")
    recover.add_argument("operation_id")
    recover.add_argument(
        "--acknowledge", action="store_true", help="Подтвердить закрытие подготовки без возобновления"
    )
    commands.add_parser("forget-session", help="Удалить локально сохранённую сессию Windows")
    return parser


def main(argv=None):
    args = arguments().parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from .gui import launch

            return launch(getattr(args, "project", None))
        if args.command == "analyze":
            result = summary(analyze(args.source, args.project, args.timezone, export_format=args.format))
        elif args.command == "report":
            result = summary(args.project)
            if args.out:
                save_report(args.out, project=args.project)
        elif args.command == "chats":
            with catalogue(args.project) as db:
                result = [dict(r) for r in db.execute("SELECT id,title,pages FROM chats ORDER BY id")]
        elif args.command == "verify":
            result = verify(args.project)
            print(json.dumps(result))
            return int(any(value for key, value in result.items() if key != "ok"))
        elif args.command == "backup":
            result = {"path": str(backup_project(args.project, args.out))}
        elif args.command == "prepare":
            bundle = prepare(
                args.project,
                args.chat,
                args.out,
                args.timezone,
                profile=args.profile,
                sample=args.sample,
                date_policy=args.dates,
                media_mode=args.media_mode,
                first_record=args.first_record,
                last_record=args.last_record,
                video_policy=args.video_policy,
                media_tools=MediaTools(args.ffmpeg, args.ffprobe),
                annotate=args.historical_notes,
                record_kind=args.record_kind,
            )
            result = {
                k: bundle.data[k]
                for k in (
                    "source_records",
                    "selected_records",
                    "included",
                    "skipped",
                    "losses",
                    "media_mode",
                    "media_count",
                    "omitted_assets",
                )
            }
        elif args.command == "fixture-series":
            from .fixtures import incremental_fixture

            result = incremental_fixture(args.out)
        elif args.command == "fixture":
            from .fixtures import prepare_fixture

            bundle = prepare_fixture(args.out, args.scenario, size=args.size)
            result = {
                "fixture": args.scenario,
                "included": bundle.data["included"],
                "media_count": bundle.data["media_count"],
            }
        elif args.command == "plan":
            data = plan_import(
                args.project,
                args.chat,
                args.timezone,
                profile=args.profile,
                sample=args.sample,
                date_policy=args.dates,
                media_mode=args.media_mode,
                first_record=args.first_record,
                last_record=args.last_record,
                video_policy=args.video_policy,
                media_tools=MediaTools(args.ffmpeg, args.ffprobe),
                annotate=args.historical_notes,
                record_kind=args.record_kind,
            )
            result = plan_summary(data)
            if args.out:
                save_report(args.out, project=args.project, package=data, planned=True)
        elif args.command == "media-check":
            from .core import properties
            from .media_engine import inspect_bundle, inspect_project, public_inventory

            protected = [local(args.path)]
            if args.project:
                with catalogue(args.path) as db:
                    protected.append(local(Path(properties(db)["original"])))
                result = inspect_project(args.path, args.chat)
            else:
                result = inspect_bundle(load_bundle(args.path), hash_verified=True)
            if not args.private:
                result = public_inventory(result)
            if args.out:
                dest = local(args.out)
                if any(dest.is_relative_to(root) for root in protected):
                    raise RecoveryError("Сохраните отчёт отдельно от пакета, проекта и источника.")
                publish_text(dest, json.dumps(result, ensure_ascii=False, indent=2))
                result = {key: value for key, value in result.items() if key != "items"}
        elif args.command == "simulate":
            bundle = load_bundle(args.bundle)
            result = rehearse(bundle, args.scenario, quiet, project=args.project)
            if "verification" in result:
                result["verification"] = redact_verification(result["verification"])
        elif args.command == "verify-import":
            bundle = load_bundle(args.bundle)
            result = compare_files(bundle, args.captures, args.mapping, project=args.project)
            record_evidence(result)
            if args.out:
                save_report(
                    args.out,
                    verification=result,
                    project=args.project,
                    protected=(bundle.root,),
                    private_details=args.private,
                )
            if not args.private:
                result = redact_verification(result)
        elif args.command == "fidelity":
            from .fidelity import fidelity, public_fidelity

            bundle = load_bundle(args.bundle)
            if len(args.captures) > 2:
                raise RecoveryError("Выберите один или два снимка.")
            comparison = compare_files(bundle, args.captures, args.mapping, project=args.project) if args.captures else None
            result = fidelity(bundle.data, comparison)
            if not args.private:
                result = public_fidelity(result)
            if args.out:
                dest = local(args.out)
                if any(dest.is_relative_to(local(root)) for root in (bundle.root, args.project) if root):
                    raise RecoveryError("Сохраните отчёт отдельно от пакета и проекта.")
                publish_text(dest, json.dumps(result, ensure_ascii=False, indent=2))
                result = {"saved": True, "records": len(result["records"]), "quality": result["quality"]}
        elif args.command in {"operations", "recover-import", "import-diagnostic"}:
            from .owner import app_home

            path = app_home() / "imports.sqlite"
            journal = Journal(path, "") if path.exists() else None
            if args.command == "import-diagnostic":
                if not journal:
                    raise RecoveryError("Журнал операций отсутствует.")
                result = journal.diagnostic_report(args.operation_id, private=args.private)
                publish_text(local(args.out), json.dumps(result, ensure_ascii=False, indent=2))
                result = {"saved": True}
            elif args.command == "recover-import":
                if not journal:
                    raise RecoveryError("Журнал операций отсутствует.")
                result = {
                    "operation_id": journal.recover_before_start(
                        args.operation_id, acknowledged=args.acknowledge
                    ),
                    "state": "STAGED_NOT_STARTED",
                }
            else:
                if journal:
                    journal.classify_interrupted()
                result = (
                    (
                        journal.events(args.events)
                        if args.events
                        else [journal.diagnostic_report(op["id"])["operation"] for op in journal.operations()]
                    )
                    if journal
                    else []
                )
        elif args.command == "forget-session":
            from .owner import vault_path

            vault_path().unlink(missing_ok=True)
            result = {"local_session_removed": True}
        else:
            from .owner import owner_run

            args_observation = (
                (args.out, args.after_id, args.through_id) if args.command == "observe" else None
            )
            options = (
                {
                    key: getattr(args, key)
                    for key in (
                        "baseline",
                        "historical_from",
                        "historical_through",
                        "max_scan",
                        "media_hash_limit",
                    )
                }
                if args_observation
                else None
            )
            result = owner_run(
                args.bundle,
                args.peer,
                observation=args_observation,
                baseline=args.command == "baseline",
                baseline_path=args.out if args.command == "baseline" else None,
                probe_media=getattr(args, "probe_media", False),
                project=getattr(args, "project", None),
                capture_options=options,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ImportFault as fault:
        print(json.dumps(fault.details(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (RecoveryError, KeyboardInterrupt, EOFError) as exc:
        print(str(exc) if isinstance(exc, RecoveryError) else "Операция остановлена.", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("Ошибка доступа к локальным данным. Проверьте пути и свободное место.", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - public error boundary, with redaction below
        # Third-party exceptions may contain private RPC arguments, file names or text.
        print("Сбой операции. Проверьте доступ к данным и совместимость зависимостей.", file=sys.stderr)
        return 2
    finally:
        if getattr(args, "pause", False) and sys.stdin.isatty():
            try:
                input("Нажмите Enter, чтобы закрыть окно.")
            except (KeyboardInterrupt, EOFError):
                pass


def record_evidence(result):
    if result["mode"] == "telegram_observation" and len(result["views"]) == 2:
        from .owner import app_home

        a, _b = result["views"]
        path = app_home() / "imports.sqlite"
        if path.is_file():
            result["operation_id"] = Journal(
                path, Endpoint.telegram(a["account_id"], a["peer_id"], a.get("peer_kind","pm"))
            ).reconcile(result)
    return result
