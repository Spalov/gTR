#include "translation.hpp"
#include "window.hpp"

#include <QCheckBox>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFileDialog>
#include <QFormLayout>
#include <QImageReader>
#include <QInputDialog>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPixmap>
#include <QPushButton>
#include <QTableWidget>

namespace {
void addDialogAction(QDialogButtonBox &buttons, QDialog &dialog, const QString &title, int action) {
    auto *button = buttons.addButton(title, QDialogButtonBox::ActionRole);
    QObject::connect(button, &QPushButton::clicked, &dialog, [&dialog, action] { dialog.done(action); });
}

bool approve(QWidget *parent, const QString &question) {
    QMessageBox dialog(QMessageBox::Question, "gTR", question, QMessageBox::Yes | QMessageBox::No, parent);
    dialog.setTextFormat(Qt::PlainText);
    dialog.setDefaultButton(QMessageBox::No);
    return dialog.exec() == QMessageBox::Yes;
}
}

void Window::showAccount() {
    task(B("Проверка аккаунта", "Перевірка облікового запису"), "account.status", {},
         [this](const QJsonObject &state) { accountDialog(state); });
}

void Window::accountDialog(const QJsonObject &state) {
    QDialog dialog(this);
    dialog.setWindowTitle(B("Аккаунт Telegram", "Обліковий запис Telegram"));
    dialog.setMinimumWidth(430);
    QFormLayout form(&dialog);
    const auto account = state["account"].toObject();
    QLabel connected(account.isEmpty() ? B("Введите данные своего приложения Telegram.", "Введіть дані свого застосунку Telegram.")
                                      : account["name"].toString());
    connected.setTextFormat(Qt::PlainText);
    connected.setWordWrap(true);
    form.addRow(&connected);
    QCheckBox savedApi(B("Использовать сохранённые реквизиты", "Використати збережені реквізити"));
    QCheckBox savedSession(B("Открыть сохранённую сессию", "Відкрити збережений сеанс"));
    savedApi.setChecked(state["saved_api"].toBool());
    savedSession.setChecked(state["saved_session"].toBool());
    if (savedApi.isChecked())
        form.addRow(&savedApi);
    if (savedSession.isChecked())
        form.addRow(&savedSession);
    QLineEdit api, hash, phone;
    hash.setEchoMode(QLineEdit::Password);
    hash.setMaxLength(32);
    phone.setMaxLength(16);
    phone.setPlaceholderText("+...");
    const auto fields = [&] {
        api.setEnabled(!savedApi.isChecked());
        hash.setEnabled(!savedApi.isChecked());
        phone.setEnabled(!savedSession.isChecked());
    };
    connect(&savedApi, &QCheckBox::toggled, &dialog, fields);
    connect(&savedSession, &QCheckBox::toggled, &dialog, fields);
    fields();
    form.addRow("API ID", &api);
    form.addRow("API Hash", &hash);
    form.addRow(B("Телефон", "Телефон"), &phone);
    QCheckBox keepApi(B("Сохранить реквизиты на этом ПК", "Зберегти реквізити на цьому ПК"));
    keepApi.setChecked(true);
    QCheckBox keepSession(B("Сохранить сессию на этом ПК", "Зберегти сеанс на цьому ПК"));
    form.addRow(&keepApi);
    form.addRow(&keepSession);
    QDialogButtonBox buttons(QDialogButtonBox::Cancel);
    addDialogAction(buttons, dialog, account.isEmpty() ? B("Подключить", "Підключити") : B("Сменить аккаунт", "Змінити обліковий запис"), 2);
    if (!account.isEmpty()) {
        addDialogAction(buttons, dialog, B("Карточка", "Картка"), 3);
        addDialogAction(buttons, dialog, B("Отключить", "Відключити"), 4);
    }
    if (state["saved_session"].toBool() || state["saved_api"].toBool())
        addDialogAction(buttons, dialog, B("Забыть данные…", "Забути дані…"), 5);
    connect(&buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    form.addRow(&buttons);
    const int action = dialog.exec();
    if (action == 2) {
        testTarget_.clear();
        account_->setText(B("Telegram · подключение", "Telegram · підключення"));
        const QJsonObject params{{"saved_api", savedApi.isChecked()}, {"saved_session", savedSession.isChecked()},
            {"api_id", api.text().toInt()}, {"api_hash", hash.text().trimmed()}, {"phone", phone.text().trimmed()},
            {"remember_api", keepApi.isChecked()}, {"remember_session", keepSession.isChecked()}};
        hash.clear();
        phone.clear();
        task(B("Подключение к Telegram", "Підключення до Telegram"), "account.connect", params,
             [this](const QJsonObject &value) { answerLogin("account", value); });
    } else if (action == 3) {
        task(B("Чтение карточки", "Читання картки"), "account.profile", {}, [this, account](const QJsonObject &profile) {
            QMessageBox box(QMessageBox::Information, "Telegram",
                account["name"].toString() + "\n" + profile["username"].toString() + "\n" +
                B("Возраст аккаунта · %1\nПоследняя активность · %2", "Вік облікового запису · %1\nОстання активність · %2")
                .arg(profile["age"].toString().isEmpty() ? B("нет данных", "немає даних") : profile["age"].toString(),
                     profile["last_seen"].toString().isEmpty() ? B("нет данных", "немає даних") : profile["last_seen"].toString()),
                QMessageBox::Ok, this);
            box.setTextFormat(Qt::PlainText);
            const auto bytes = QByteArray::fromBase64(profile["avatar"].toString().toLatin1());
            if (QPixmap image; !bytes.isEmpty() && image.loadFromData(bytes))
                box.setIconPixmap(image.scaled(76, 76, Qt::KeepAspectRatio, Qt::SmoothTransformation));
            box.exec();
        });
    } else if (action == 4 || (action == 5 && approve(this,
            B("Удалить сохранённые реквизиты и сессию этого ПК?", "Видалити збережені реквізити й сеанс цього ПК?")))) {
        task(B("Отключение сессии", "Відключення сеансу"), action == 5 ? "account.forget" : "account.disconnect",
             action == 5 ? QJsonObject{{"confirmed", true}, {"api", true}} : QJsonObject{},
             [this](const QJsonObject &) {
                 testTarget_.clear();
                 account_->setText(B("Telegram · локальный режим", "Telegram · локальний режим"));
             });
    }
}

void Window::answerLogin(const QString &scope, const QJsonObject &value) {
    if (scope == "test" && value["reset"].toInteger() > 0) {
        testTarget_ = value["target"].toString();
        testBundle_.clear();
        status(B("Пустая история подтверждена обоими аккаунтами. Подготовьте тестовый пакет.",
                 "Порожню історію підтверджено обома обліковими записами. Підготуйте тестовий пакет."));
        return;
    }
    const auto challenge = value["challenge"].toString();
    if (challenge == "code" || challenge == "password") {
        bool ok = false;
        auto secret = QInputDialog::getText(this, "Telegram", challenge == "code"
            ? B("Код подтверждения Telegram", "Код підтвердження Telegram")
            : B("Пароль двухэтапной проверки", "Пароль двоетапної перевірки"), QLineEdit::Password, {}, &ok);
        if (ok && !secret.isEmpty()) {
            task(B("Проверка входа", "Перевірка входу"), scope + "." + challenge, {{"value", secret}},
                 [this, scope](const QJsonObject &result) { answerLogin(scope, result); });
            secret.clear();
        } else {
            task(B("Отмена входа", "Скасування входу"), scope == "test" ? "test.cancel" : "account.disconnect", {},
                 [this, scope](const QJsonObject &) {
                     if (scope == "account")
                         account_->setText(B("Telegram · локальный режим", "Telegram · локальний режим"));
                 });
        }
        return;
    }
    if (const auto account = value["account"].toObject(); !account.isEmpty())
        account_->setText(B("Telegram · %1", "Telegram · %1").arg(account["name"].toString()));
}

void Window::selectTarget(int offset, const QString &query, bool refresh) {
    const auto open = [this, offset, query, refresh] {
        task(B("Чтение диалогов", "Читання діалогів"), "account.dialogs",
             {{"refresh", refresh}, {"offset", offset}, {"query", query}}, [this, offset, query](const QJsonObject &list) {
            QDialog dialog(this);
            dialog.setWindowTitle(B("Целевой диалог Telegram", "Цільовий діалог Telegram"));
            QFormLayout form(&dialog);
            QLineEdit search(query);
            search.setMaxLength(120);
            QComboBox dialogs;
            for (const auto &entry : list["dialogs"].toArray()) {
                const auto row = entry.toObject();
                dialogs.addItem(row["name"].toString() + " · " + row["id"].toString(), row["id"].toString());
            }
            dialogs.setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
            dialogs.setMinimumContentsLength(26);
            form.addRow(B("Фильтр по названию", "Фільтр за назвою"), &search);
            form.addRow(B("Диалог", "Діалог"), &dialogs);
            QDialogButtonBox buttons(QDialogButtonBox::Cancel);
            if (dialogs.count())
                addDialogAction(buttons, dialog, B("Выбрать", "Вибрати"), 2);
            addDialogAction(buttons, dialog, B("Найти", "Знайти"), 3);
            if (offset + 150 < list["total"].toInt())
                addDialogAction(buttons, dialog, B("Далее", "Далі"), 4);
            if (offset > 0)
                addDialogAction(buttons, dialog, B("Назад", "Назад"), 5);
            connect(&search, &QLineEdit::returnPressed, &dialog, [&dialog] { dialog.done(3); });
            connect(&buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
            form.addRow(&buttons);
            const int action = dialog.exec();
            if (action == 2)
                transfer(dialogs.currentData().toString(), dialogs.currentText());
            if (action >= 3)
                selectTarget(action == 4 ? offset + 150 : action == 5 ? offset - 150 : 0, search.text().trimmed(), false);
        });
    };
    if (refresh) {
        task(B("Проверка аккаунта", "Перевірка облікового запису"), "account.status", {},
             [this, open](const QJsonObject &value) {
                 if (value["account"].isObject())
                     open();
                 else
                     accountDialog(value);
             });
    } else {
        open();
    }
}

void Window::transfer(const QString &target, const QString &name) {
    task(B("Чтение профиля импорта", "Читання профілю імпорту"), "profile.use", {{"target", target}},
         [this, target, name](const QJsonObject &profile) { transferActions(target, name, profile); });
}

void Window::transferActions(const QString &target, const QString &name, const QJsonObject &profile) {
    QDialog dialog(this);
    dialog.setWindowTitle(B("Восстановление в Telegram", "Відновлення в Telegram"));
    QVBoxLayout layout(&dialog);
    QLabel destination(name);
    destination.setTextFormat(Qt::PlainText);
    layout.addWidget(&destination);
    const auto pending = profile["recoverable_preparation"].toObject();
    const auto interrupted = pending["operation_id"].toString();
    QLabel pendingStatus;
    if (!interrupted.isEmpty()) {
        pendingStatus.setText(B(
            "Прервана подготовка. Telegram принял %1 из %2 вложений. Финальный запрос не отправлен.",
            "Підготовку перервано. Telegram прийняв %1 із %2 вкладень. Фінальний запит не надіслано.")
            .arg(pending["media_done"].toInteger()).arg(pending["media"].toInteger()));
        pendingStatus.setWordWrap(true);
        layout.addWidget(&pendingStatus);
    }
    const bool partial = plan_["selection"].toObject()["partial"].toBool();
    QCheckBox consent(partial
        ? B("Участники согласовали импорт выбранной части истории", "Учасники погодили імпорт вибраної частини історії")
        : B("Участники согласовали импорт этого пакета в диалог", "Учасники погодили імпорт цього пакета в діалог"));
    layout.addWidget(&consent);
    QCheckBox verifyPrevious(B("Проверять предыдущие операции", "Перевіряти попередні операції"));
    verifyPrevious.setChecked(profile["verify_previous"].toBool(true));
    layout.addWidget(&verifyPrevious);
    QCheckBox test(B("Тестовый режим для двух собственных аккаунтов", "Тестовий режим для двох власних облікових записів"));
    test.setChecked(testTarget_ == target && !testTarget_.isEmpty());
    layout.addWidget(&test);
    QComboBox actions;
    actions.setAccessibleName(B("Действие", "Дія"));
    actions.addItem(B("Проверить пакет и диалог", "Перевірити пакет і діалог"), 2);
    actions.addItem(B("Подготовить только новые записи", "Підготувати лише нові записи"), 3);
    actions.addItem(B("Сохранить границу перед импортом…", "Зберегти межу перед імпортом…"), 4);
    actions.addItem(B("Сохранить снимок по границе…", "Зберегти знімок за межею…"), 5);
    actions.addItem(B("Сохранить параметры профиля", "Зберегти параметри профілю"), 6);
    actions.addItem(B("Сбросить реестр тестовых импортов…", "Скинути реєстр тестових імпортів…"), 7);
    if (test.isChecked())
        actions.addItem(B("Подготовить тестовый пакет", "Підготувати тестовий пакет"), 8);
    if (!rejectedMedia_["decisions"].toArray().isEmpty())
        actions.addItem(B("Обработка отклонённых вложений…", "Обробка відхилених вкладень…"), 9);
    if (!interrupted.isEmpty())
        actions.addItem(B("Закрыть прерванную подготовку…", "Закрити перервану підготовку…"), 10);
    actions.addItem(B("Сохранить снимок по датам…", "Зберегти знімок за датами…"), 11);
    if (!interrupted.isEmpty())
        actions.setCurrentIndex(actions.findData(10));
    else if (test.isChecked() && bundle_ != testBundle_)
        actions.setCurrentIndex(actions.findData(8));
    else if (!test.isChecked() && !verifyPrevious.isChecked())
        actions.setCurrentIndex(actions.findData(3));
    layout.addWidget(&actions);
    QDialogButtonBox buttons(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    connect(&buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(&buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout.addWidget(&buttons);
    QJsonObject params{{"target", target}, {"bundle", bundle_}, {"project", project_}};
    if (dialog.exec() != QDialog::Accepted)
        return;
    const auto action = actions.currentData().toInt();
    if (action == 2) {
        if (!consent.isChecked()) {
            status(B("Подтвердите согласие участников для этого пакета.", "Підтвердьте згоду учасників для цього пакета."));
            return;
        }
        params["consented"] = true;
        params["test"] = test.isChecked();
        if (!manifest_.isEmpty())
            params["manifest"] = manifest_;
        task(B("Проверка пакета и диалога", "Перевірка пакета й діалогу"), "import.validate", params,
             [this](const QJsonObject &value) { startImport(value); });
    } else if (action == 3 || action == 8) {
        params["confirmed"] = true;
        if (action == 8)
            params["test"] = true;
        task(B("Подготовка пакета", "Підготовка пакета"), action == 3 ? "import.new" : "import.test_package", params,
             [this, action](const QJsonObject &value) {
                 bundle_ = value["path"].toString();
                 if (action == 8)
                     testBundle_ = bundle_;
                 manifest_ = value["manifest"].toString();
                 result_->setText(B("Новый пакет · %1 сообщений", "Новий пакет · %1 повідомлень").arg(value["included"].toInteger()));
             });
    } else if (action == 4 || action == 5 || action == 11) {
        const auto path = QFileDialog::getSaveFileName(this, B("Сохранить локальный JSON", "Зберегти локальний JSON"),
                                                      {}, "JSON (*.json)", nullptr, QFileDialog::DontConfirmOverwrite);
        if (path.isEmpty())
            return;
        params["path"] = path;
        if (action == 5) {
            const auto baseline = QFileDialog::getOpenFileName(this, B("Выберите границу до импорта", "Виберіть межу до імпорту"),
                                                               {}, "JSON (*.json)");
            if (baseline.isEmpty())
                return;
            params["after"] = 0;
            params["baseline"] = baseline;
        } else if (action == 11) {
            bool accepted = false;
            const auto first = QInputDialog::getText(this, "gTR",
                B("Первая историческая дата UTC (ГГГГ-ММ-ДД)", "Перша історична дата UTC (РРРР-ММ-ДД)"),
                QLineEdit::Normal, {}, &accepted);
            if (!accepted)
                return;
            const auto last = QInputDialog::getText(this, "gTR",
                B("Последняя историческая дата UTC (ГГГГ-ММ-ДД)", "Остання історична дата UTC (РРРР-ММ-ДД)"),
                QLineEdit::Normal, first, &accepted);
            if (!accepted)
                return;
            params["after"] = 0;
            params["first_date"] = first.trimmed();
            params["last_date"] = last.trimmed();
        }
        task(B("Чтение истории Telegram", "Читання історії Telegram"), action == 4 ? "import.baseline" : "import.observe",
             params, [](const QJsonObject &) {});
    } else if (action == 6) {
        task(B("Сохранение профиля", "Збереження профілю"), "profile.preference",
             {{"target", target}, {"verify_previous", verifyPrevious.isChecked()}}, [](const QJsonObject &) {});
    } else if (action == 7 && test.isChecked() && approve(this, B(
            "Сброс относится к выбранному диалогу. Подтверждаете, что оба аккаунта ваши и история удалена у обоих?",
            "Скидання стосується вибраного діалогу. Підтверджуєте, що обидва облікові записи ваші й історію видалено в обох?"))) {
        bool ok = false;
        const auto phone = QInputDialog::getText(this, "Telegram", B("Телефон второго аккаунта", "Телефон другого облікового запису"),
                                                 QLineEdit::Normal, {}, &ok);
        if (ok && !phone.isEmpty())
            task(B("Проверка второго аккаунта", "Перевірка другого облікового запису"), "test.begin",
                 {{"target", target}, {"phone", phone.trimmed()}, {"confirmed", true}},
                 [this](const QJsonObject &value) { answerLogin("test", value); });
    } else if (action == 9) {
        repairRejectedMedia(params);
    } else if (action == 10 && !interrupted.isEmpty() && approve(this, B(
            "Закрыть прерванную подготовку? Для новой попытки потребуется проверка пакета и ваше подтверждение.",
            "Закрити перервану підготовку? Для нової спроби знадобиться перевірка пакета і ваше підтвердження."))) {
        task(B("Проверка прежней операции", "Перевірка попередньої операції"), "import.recover",
             {{"target", target}, {"operation_id", interrupted}, {"confirmed", true}}, [this](const QJsonObject &) {
                 status(B("Подготовка закрыта. Откройте серверный импорт и выберите проверку пакета и диалога.",
                          "Підготовку закрито. Відкрийте серверний імпорт і виберіть перевірку пакета й діалогу."));
             });
    }
}

void Window::startImport(const QJsonObject &review) {
    const auto text = B("Аккаунт %1\nДиалог %2 · %3\nСообщения %4 · вложения %5\n\n%6\n\nНачать импорт в общий диалог?",
                        "Обліковий запис %1\nДіалог %2 · %3\nПовідомлення %4 · вкладення %5\n\n%6\n\nПочати імпорт у спільний діалог?")
        .arg(review["account"].toString(), review["target"].toString(), review["target_id"].toString(),
             QString::number(review["messages"].toInteger()), QString::number(review["attachments"].toInteger()),
             review["confirmation"].toString());
    if (!approve(this, text)) {
        task(B("Закрытие проверки", "Закриття перевірки"), "import.discard", {}, [](const QJsonObject &) {});
        return;
    }
    task(B("Серверный импорт", "Серверний імпорт"), "import.execute", {{"confirmed", true}, {"lease", review["lease"]}},
         [this](const QJsonObject &) {
             result_->setText(B("Telegram принял запуск. Снимите историю с обеих сторон и выполните сверку.",
                                "Telegram прийняв запуск. Збережіть знімки історії з обох сторін і виконайте звірку."));
         });
}

void Window::repairRejectedMedia(const QJsonObject &params) {
    QDialog dialog(this);
    dialog.setWindowTitle(B("Выбрать обработку вложений", "Вибрати обробку вкладень"));
    QFormLayout form(&dialog);
    QList<std::pair<QString, QComboBox *>> entries;
    for (const auto &value : rejectedMedia_["decisions"].toArray()) {
        const auto row = value.toObject();
        auto *file = new QLabel(row["name"].toString(), &dialog);
        file->setTextFormat(Qt::PlainText);
        file->setWordWrap(true);
        file->setToolTip(B("Архив · %1\nПакет · %2\nTelegram · %3", "Архів · %1\nПакет · %2\nTelegram · %3")
            .arg(row["source_kind"].toString(), row["prepared_kind"].toString(), row["received_kind"].toString()));
        auto *choices = new QComboBox(&dialog);
        choices->addItem(B("Выберите действие", "Виберіть дію"), "");
        for (const auto &option : row["options"].toArray()) {
            const auto key = option.toString();
            const auto text = key == "document" ? B("Оригинал как документ", "Оригінал як документ")
                : key == "skip" ? B("Пропустить вложение", "Пропустити вкладення")
                : key == "local" ? B("Оставить локально", "Залишити локально")
                : key == "original" ? B("Подготовить оригинал", "Підготувати оригінал")
                : B("Совместимая копия", "Сумісна копія");
            choices->addItem(text, key);
        }
        if (const int suggested = choices->findData(row["recommended"].toString()); suggested >= 0)
            choices->setCurrentIndex(suggested);
        form.addRow(file, choices);
        entries.append({row["key"].toString(), choices});
    }
    QDialogButtonBox buttons(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    form.addRow(&buttons);
    connect(&buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(&buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    if (dialog.exec() != QDialog::Accepted)
        return;
    auto input = params;
    QJsonObject decisions;
    for (const auto &[key, choices] : entries)
        if (!choices->currentData().toString().isEmpty())
            decisions[key] = choices->currentData().toString();
    if (decisions.isEmpty())
        return;
    input["decisions"] = decisions;
    input["confirmed"] = true;
    const bool testPackage = !bundle_.isEmpty() && bundle_ == testBundle_;
    task(B("Подготовка совместимых вложений", "Підготовка сумісних вкладень"), "import.repair", input,
         [this, testPackage](const QJsonObject &value) {
             bundle_ = value["path"].toString();
             if (testPackage)
                 testBundle_ = bundle_;
             manifest_ = value["manifest"].toString();
             rejectedMedia_ = {};
             result_->setText(B("Исправленный пакет подготовлен. Повторите проверку перед запуском.",
                                "Виправлений пакет підготовлено. Повторіть перевірку перед запуском."));
         });
}

void Window::operationDiagnostic(bool privateDetails) {
    const auto *row = history_->item(history_->currentRow(), 0);
    if (!row)
        return;
    if (privateDetails && !approve(this, B("Отчёт может содержать локальные сведения. Сохранить подробный отчёт?",
                                           "Звіт може містити локальні відомості. Зберегти докладний звіт?")))
        return;
    task(B("Чтение отчёта", "Читання звіту"), "operation.report",
         {{"operation_id", row->data(Qt::UserRole).toString()}, {"private", privateDetails}},
         [this](const QJsonObject &value) {
             lastDiagnostic_ = value;
             saveDiagnostic_->show();
             saveValidationDiagnostic();
         });
}

void Window::operationBundle() {
    const auto *row = history_->item(history_->currentRow(), 0);
    if (!row)
        return;
    task(B("Открытие сохранённого пакета", "Відкриття збереженого пакета"), "operation.open",
         {{"operation_id", row->data(Qt::UserRole).toString()}}, [this](const QJsonObject &value) {
             historyBundle_ = value["path"].toString();
             diagnosis_->setText(B("Пакет операции готов к сверке с двумя снимками.",
                                   "Пакет операції готовий до звірки з двома знімками."));
         });
}
