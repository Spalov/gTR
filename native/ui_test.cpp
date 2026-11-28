#include "translation.hpp"
#include "window.hpp"

#include <QApplication>
#include <QComboBox>
#include <QDir>
#include <QFile>
#include <QListView>
#include <QListWidget>
#include <QMessageBox>
#include <QPixmap>
#include <QPushButton>
#include <QScrollArea>
#include <QSettings>
#include <QStackedWidget>
#include <QTemporaryDir>
#include <QTest>

#include <functional>
#include <iostream>

namespace {
bool waitFor(const std::function<bool()> &predicate) {
    for (int attempt = 0; attempt < 800; ++attempt) {
        if (predicate())
            return true;
        QTest::qWait(10);
    }
    return false;
}

QJsonObject call(ServiceClient &service, const QString &method, const QJsonObject &params) {
    QJsonObject value;
    bool received = false;
    service.send(method, params, [&](const QJsonObject &data, const QString &error) {
        if (!error.isEmpty())
            std::cerr << method.toStdString() << ": " << error.toStdString() << '\n';
        value = data;
        received = true;
    });
    if (!waitFor([&] { return received; }))
        service.stop();
    return value;
}

void choose(QMessageBox::StandardButton choice) {
    QTimer::singleShot(60, [choice] {
        if (auto *message = qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
            if (auto *control = message->button(choice))
                control->click();
        }
    });
}

int fail(const char *reason) {
    std::cerr << reason << '\n';
    return 1;
}
}

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setStyle("Fusion");
    app.setApplicationName("gTR-Test");
    app.setOrganizationName("gAssets-Tests");
    const auto args = app.arguments();
    if (args.size() != 5)
        return fail("Expected root, Python, resolution and scale");
    const auto root = QDir(args[1]).absolutePath();
    const auto python = QDir::cleanPath(QDir().absoluteFilePath(args[2]));
    const auto dimensions = args[3].split('x');
    const auto scale = args[4].toInt();
    const int width = dimensions.at(0).toInt() / scale;
    const int height = dimensions.at(1).toInt() / scale;
    Ukrainian translator;
    if (scale == 2)
        app.installTranslator(&translator);

    QTemporaryDir sandbox;
    if (!sandbox.isValid())
        return fail("Temporary directory required");
    const auto isolated = sandbox.path();
    qputenv("LOCALAPPDATA", isolated.toUtf8());
    qunsetenv("GTR_UPDATE_BOOT_FILE");
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, isolated);
    QSettings().setValue("language", scale == 2 ? "uk" : "ru");

    QFile source(sandbox.filePath("example.json"));
    if (!source.open(QIODevice::WriteOnly))
        return fail("Unable to write synthetic export");
    source.write(R"({"id":42,"name":"Archive check","type":"personal_chat","messages":[
        {"id":1,"type":"message","from":"Tester","from_id":"user1","date":"2026-01-01T10:00:00+02:00","text":"First"},
        {"id":2,"type":"message","from":"Tester","from_id":"user1","date":"2026-01-01T10:00:00+02:00","text":"Second","reply_to_message_id":1}
        ]})");
    source.close();
    ServiceClient client(root, python, &app);
    client.start();
    if (!waitFor([&] { return client.available(); }))
        return fail("Python protocol handshake did not complete");
    const auto value = call(client, "archive.analyze", {{"source", source.fileName()}});
    client.stop();
    const auto project = value["path"].toString();
    if (project.isEmpty())
        return fail("Python did not open the synthetic export");

    Window window(root, python, project);
    window.resize(width, height);
    window.show();
    auto *nav = window.findChild<QListWidget *>("nav");
    auto *pages = window.findChild<QStackedWidget *>("pages");
    auto *records = window.findChild<QListView *>("archive-records");
    auto *steps = window.findChild<QListWidget *>("steps");
    auto *next = window.findChild<QPushButton *>("wizard-next");
    auto *prepare = window.findChild<QPushButton *>("prepare-bundle");
    if (!nav || !pages || !records || !steps || !next || !prepare)
        return fail("Native widgets are unavailable");
    if (!waitFor([&] { return records->model()->rowCount() == 3; }))
        return fail("Native viewer did not receive the expected two records and date divider");
    if (window.width() > width || window.height() > height)
        return fail("Window minimum exceeds the requested resolution and scale");
    for (int index = 0; index < pages->count(); ++index) {
        nav->setCurrentRow(index);
        QTest::qWait(40);
        const auto *scroll = qobject_cast<QScrollArea *>(pages->currentWidget());
        if (!scroll || scroll->widget()->width() > scroll->viewport()->width() + 1) {
            if (scroll)
                std::cerr << "Page " << index << ", content " << scroll->widget()->width()
                          << ", viewport " << scroll->viewport()->width() << '\n';
            return fail("Native page is clipped horizontally");
        }
        if (index == 1 && width == 1366 && scale == 1) {
            const auto screenshot = qEnvironmentVariable("GTR_NATIVE_TEST_SCREENSHOT");
            if (!screenshot.isEmpty() && !window.grab().save(screenshot, "PNG"))
                return fail("Unable to save the requested synthetic screenshot");
        }
    }
    if (!waitFor([&] { return pages->isEnabled(); }))
        return fail("UI did not finalize the storage task");
    nav->setCurrentRow(2);
    next->click();
    next->click();
    if (!waitFor([&] { return steps->currentRow() == 2 && next->isEnabled(); }))
        return fail("Native wizard did not analyze the selected chat");
    next->click();
    choose(QMessageBox::No);
    prepare->click();
    const auto packages = sandbox.filePath("gAssets/gTR/packages");
    if (steps->currentRow() != 3 || QFileInfo::exists(packages))
        return fail("Declining the local package did not stop preparation");
    choose(QMessageBox::Yes);
    prepare->click();
    if (!waitFor([&] { return steps->currentRow() == 4; }))
        return fail("Native wizard did not finish the local package");
    if (QDir(packages).entryList(QDir::Dirs | QDir::NoDotAndDotDot).size() != 1)
        return fail("A single package should have been published");
    window.close();
}
