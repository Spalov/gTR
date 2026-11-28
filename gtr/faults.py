"""Structured, redacted failures for both owner and simulated transport."""

from dataclasses import asdict, dataclass

from .model import RecoveryError
from .package_errors import PACKAGE_ERRORS, public_package_issue


@dataclass(frozen=True)
class FaultPolicy:
    message: str
    technical: str
    action: str
    retry: str = "after_review"


POLICIES = {
    **{code: FaultPolicy(message, message, action) for code, (message, action) in PACKAGE_ERRORS.items()},
    "LOCAL_VALIDATION_FAILED": FaultPolicy(
        "Движок остановил проверку пакета.",
        "Нарушено ожидаемое состояние на указанном этапе проверки.",
        "Сохраните обезличенный отчёт и повторите локальную проверку перед продолжением.",
    ),
    "IMPORT_FORMAT_UNRECOGNIZED": FaultPolicy(
        "Telegram не распознал формат.",
        "Отказ распознавателя истории.",
        "Проверьте профиль на короткой выборке.",
    ),
    "IMPORT_FORMAT_DATE_INVALID": FaultPolicy(
        "Telegram отклонил дату.", "Дата отвергнута при инициализации.", "Проверьте формат и план дат."
    ),
    "IMPORT_FILE_INVALID": FaultPolicy(
        "Telegram отклонил файл истории.", "Отказ при разборе полного файла.", "Сравните файл и манифест."
    ),
    "USER_NOT_MUTUAL_CONTACT": FaultPolicy(
        "Для личного чата нужен взаимный контакт.",
        "Проверка получателя возвращает отказ.",
        "Проверьте контакт в Telegram.",
    ),
    "CHAT_ADMIN_REQUIRED": FaultPolicy(
        "У аккаунта недостаточно прав.",
        "Telegram требует административное право.",
        "Проверьте права в группе.",
    ),
    "PEER_ID_INVALID": FaultPolicy(
        "Целевой чат недоступен.", "Не удалось проверить идентификатор чата.", "Выберите доступный диалог."
    ),
    "SAVED_DIALOG_UNAVAILABLE": FaultPolicy(
        "Telegram не вернул сохранённый личный диалог.",
        "Список контактов не подтвердил, что сохранённый личный диалог доступен.",
        "Добавьте второй аккаунт в контакты Telegram и снова откройте список диалогов.",
    ),
    "TEST_RESET_REQUIRED": FaultPolicy(
        "Для тестового импорта нужно подтвердить пустую историю обоих аккаунтов.",
        "Для выбранного личного диалога нет подтверждённого тестового цикла в текущем сеансе.",
        "Выберите «Сбросить реестр тестовых импортов…» и подтвердите пустую историю на обоих аккаунтах.",
    ),
    "TEST_PACKAGE_REQUIRED": FaultPolicy(
        "Для текущего тестового цикла нужен тестовый пакет.",
        "Манифест пакета не связан с подтверждённым тестовым циклом.",
        "Выберите «Подготовить тестовый пакет», затем снова проверьте пакет и диалог.",
    ),
    "TEST_HISTORY_NOT_EMPTY": FaultPolicy(
        "У одного из аккаунтов в истории диалога ещё есть сообщения.",
        "Сброс реестра доступен после проверки пустой истории с обеих сторон.",
        "Проверьте историю на обоих тестовых аккаунтах и повторите проверку.",
    ),
    "PEER_TYPE": FaultPolicy(
        "Тип чата расходится с типом истории.",
        "Профиль и целевой чат несовместимы.",
        "Выберите чат подходящего типа.",
    ),
    "IMPORT_ID_INVALID": FaultPolicy(
        "Сеанс импорта недоступен.",
        "Telegram не принимает текущий идентификатор сессии импорта.",
        "Проверьте состояние операции.",
    ),
    "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT": FaultPolicy(
        "Telegram уже обрабатывает импорт.",
        "Сервер сообщил об активном импорте.",
        "Выдержите интервал, указанный сервером.",
        "after_wait",
    ),
    "FLOOD_WAIT": FaultPolicy(
        "Telegram ограничил частоту запросов.",
        "Сервер вернул время ожидания.",
        "Выдержите указанный интервал.",
        "after_wait",
    ),
    "CONNECTION_LOST": FaultPolicy(
        "Соединение с Telegram прервано.",
        "Транспорт потерял соединение.",
        "Проверьте соединение и состояние задания.",
    ),
    "TIMEOUT": FaultPolicy(
        "Истекло время ожидания Telegram.",
        "Истёк ограниченный срок ожидания ответа.",
        "Проверьте соединение и состояние задания.",
    ),
    "MEDIA_INVALID": FaultPolicy(
        "Telegram отклонил вложение.",
        "Сервер не принял InputMedia.",
        "Сохраните диагностический отчёт и проверьте тип и исходный файл. Для другого представления подготовьте новый пакет.",
        "blocked",
    ),
    "UNRESOLVED_ATTACHMENT": FaultPolicy(
        "Связь между записью и вложением не прошла проверку.",
        "Имя в истории, файле или запросе загрузки не соответствует манифесту.",
        "Сохраните журнал и подготовьте отдельный новый пакет из исходного проекта.",
        "blocked",
    ),
    "MEDIA_TYPE_CHANGED": FaultPolicy(
        "Telegram вернул вложение другого типа. Старт импорта остановлен.",
        "Тип принятого вложения отличается от подготовленного.",
        "Сверьте исходный и возвращённый типы в журнале. Выберите совместимую копию, документ или пропуск.",
        "blocked",
    ),
    "MEDIA_LOCAL_INVALID": FaultPolicy(
        "Вложение не прошло локальную проверку.",
        "Отправка пакета остановлена до сетевых проверок.",
        "Сверьте исходный файл и отчёт. Если байты нужно сохранить без специального представления, создайте отдельный пакет с медиа в виде документов.",
    ),
    "MEDIA_SIZE_LIMIT": FaultPolicy(
        "Вложение превышает доступный предел.",
        "Проверка размера файла отклонена до загрузки.",
        "Подготовьте меньшую выборку или проверьте допустимый размер.",
    ),
    "SERVER_FAILURE": FaultPolicy(
        "Telegram вернул внутреннюю ошибку.",
        "Ответ RPC класса 5xx.",
        "Проверьте состояние перед новой попыткой.",
    ),
    "RPC_REJECTED": FaultPolicy(
        "Telegram отклонил запрос.",
        "Метод завершился отрицательным результатом или известным отказом RPC.",
        "Проверьте этап и состояние диалога.",
    ),
    "START_UNCERTAIN": FaultPolicy(
        "Исход запуска неизвестен.",
        "Финальный запрос мог быть обработан без ответа.",
        "Прочитайте фактическую историю с обеих сторон.",
        "blocked",
    ),
    "STAGING_UNCERTAIN": FaultPolicy(
        "Исход подготовки на сервере неизвестен.",
        "Ответ потерян после начала серверной операции.",
        "Сверьте историю и серверное состояние.",
        "blocked",
    ),
    "PHONE_CODE_INVALID": FaultPolicy(
        "Код входа не принят.", "Код неверен или истёк.", "Повторите авторизацию с актуальным кодом."
    ),
    "PASSWORD_HASH_INVALID": FaultPolicy(
        "Пароль двухэтапной проверки не принят.",
        "Проверка пароля Telegram не прошла.",
        "Проверьте пароль и повторите ввод.",
    ),
    "AUTH_KEY_UNREGISTERED": FaultPolicy(
        "Telegram больше не принимает эту сессию.",
        "Ключ сессии отклонён сервером.",
        "Подключитесь с новой сессией.",
    ),
    "LOCAL_INVALID": FaultPolicy(
        "Локальная проверка не пройдена.",
        "Исходные данные или пакет не прошли контроль.",
        "Проверьте проект, пакет и журнал.",
    ),
    "REPLAY_BLOCKED": FaultPolicy(
        "Импорт остановлен защитой от повтора.",
        "Выбранные записи связаны с предыдущими операциями.",
        "Откройте совпадения и проверьте доступную выборку.",
        "blocked",
    ),
    "LOCAL_JOURNAL_UNAVAILABLE": FaultPolicy(
        "Не удалось записать состояние операции.",
        "Локальный журнал недоступен.",
        "Проверьте свободное место и откройте журнал. Сначала выясните состояние операции.",
        "blocked",
    ),
}
FAULTS = {code: policy.message for code, policy in POLICIES.items()}
UNSUPPORTED = {
    "IMPORT_FORMAT_UNRECOGNIZED",
    "IMPORT_FORMAT_DATE_INVALID",
    "IMPORT_FILE_INVALID",
    "USER_NOT_MUTUAL_CONTACT",
    "CHAT_ADMIN_REQUIRED",
    "PEER_ID_INVALID",
    "PEER_TYPE",
}
RESTRICTIONS = UNSUPPORTED | {
    "FLOOD_WAIT",
    "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT",
    "MEDIA_INVALID",
    "MEDIA_SIZE_LIMIT",
}
AMBIGUOUS_RPC = {"CONNECTION_LOST", "TIMEOUT", "SERVER_FAILURE"}


class ImportFault(RecoveryError):
    def __init__(
        self,
        code: str,
        seconds: int = 0,
        *,
        stage: str = "",
        retry: str | None = None,
        response_constructor: str | None = None,
        package_issue: dict | None = None,
    ):
        self.code, self.seconds, self.stage = code, seconds, stage
        self.policy = POLICIES[code]
        self.retry = retry or self.policy.retry
        self.response_constructor = response_constructor
        self.package_issue = package_issue
        self.media_attempt: dict | None = None
        self.media_issues: list[dict] = []
        self.operation_id: str | None = None
        super().__init__(self.policy.message + (f" Ожидание {seconds} с." if seconds else ""))

    def at(self, stage: str) -> "ImportFault":
        return ImportFault(
            self.code,
            self.seconds,
            stage=self.stage or stage,
            retry=self.retry,
            response_constructor=self.response_constructor,
            package_issue=self.package_issue,
        )

    @classmethod
    def from_package(cls, error):
        return cls(error.code, stage=error.stage, package_issue=error.details(private=True))

    def details(self, *, private=False) -> dict:
        result = {
            **asdict(self.policy),
            "code": self.code,
            "stage": self.stage,
            "wait_seconds": self.seconds,
            "retry": self.retry,
            "retry_allowed": self.retry != "blocked",
            "status_code": "FORMAT_UNSUPPORTED"
            if self.code
            in {"IMPORT_FORMAT_UNRECOGNIZED", "IMPORT_FORMAT_DATE_INVALID", "IMPORT_FILE_INVALID"}
            else self.code,
        }
        if self.operation_id:
            result["operation_id"] = self.operation_id
        if self.media_attempt:
            from .journal import redact_attempt

            result["media_attempt"] = self.media_attempt if private else redact_attempt(self.media_attempt)
        if self.media_issues:
            result["media_issue_count"] = len(self.media_issues)
        if self.package_issue:
            result["package_issue"] = self.package_issue if private else public_package_issue(self.package_issue)
        return result
