#include "release.hpp"
#include "translation.hpp"
#include "window.hpp"

#include <QApplication>
#include <QCommandLineParser>
#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QIcon>
#include <QImageReader>
#include <QLibraryInfo>
#include <QLocalServer>
#include <QLocalSocket>
#include <QMessageBox>
#include <QRegularExpression>
#include <QSettings>
#include <QTemporaryDir>
#include <QTimer>

#ifdef Q_OS_WIN
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#include <shobjidl.h>
#endif

namespace {
int health(QApplication &app, const QString &root, const QString &python) {
    QTemporaryDir isolated;
    if (!isolated.isValid())
        return 4;
    qputenv("LOCALAPPDATA", QDir::toNativeSeparators(isolated.path()).toUtf8());
    ServiceClient service(root, python, &app);
    service.onReady = [&] {
        service.send("project.list", {}, [&](const QJsonObject &value, const QString &error) {
            const bool clean = error.isEmpty() && value["projects"].isArray() && !value["saved_session"].toBool();
            app.exit(clean ? 0 : 4);
        });
    };
    service.onFailure = [&](const QString &) { app.exit(4); };
    QTimer::singleShot(15000, &app, [&] { app.exit(4); });
    service.start();
    const int status = app.exec();
    service.stop();
    return status;
}
}

int main(int argc, char **argv) {
#ifdef Q_OS_WIN
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_APPLICATION_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    SetCurrentProcessExplicitAppUserModelID(L"gAssets.gtr");
#endif
    QCoreApplication::setApplicationName("gTR");
    QCoreApplication::setOrganizationName("gAssets");
    QCoreApplication::setApplicationVersion(GTR_RELEASE_LABEL);
    QCoreApplication::setAttribute(Qt::AA_DontUseNativeMenuBar);
    qunsetenv("QT_PLUGIN_PATH");
    qunsetenv("QT_QPA_PLATFORM_PLUGIN_PATH");
    qputenv("QT_MEDIA_BACKEND", "windows");
    QApplication app(argc, argv);
    app.setStyle("Fusion");
    QImageReader::setAllocationLimit(64);

    QSettings settings;
    const auto language = settings.value("language", "ru").toString();
    Ukrainian ukrainian;
    QTranslator standard;
    if (language == "uk")
        app.installTranslator(&ukrainian);
    if (standard.load("qtbase_" + language, QLibraryInfo::path(QLibraryInfo::TranslationsPath)))
        app.installTranslator(&standard);

    QCommandLineParser args;
    args.addVersionOption();
    args.addOption(QCommandLineOption("root", B("Папка исходников для разработки", "Тека вихідного коду для розробки"), "path"));
    args.addOption(QCommandLineOption("project", B("Открыть локальный проект", "Відкрити локальний проєкт"), "path"));
    args.addOption(QCommandLineOption("health-check"));
    args.addOption(QCommandLineOption("diagnostic-profile",
        B("Изолированный тестовый профиль", "Ізольований тестовий профіль"), "id"));
    args.process(app);
    QDir dir(app.applicationDirPath());
    if (args.isSet("root")) {
        dir = QDir(args.value("root"));
    } else {
        for (int depth = 0; depth < 4; ++depth)
            dir.cdUp();
    }
    const auto executable = dir.filePath("runtime/python.exe");
    const auto python = QFileInfo::exists(executable) ? executable : dir.filePath(".venv/Scripts/python.exe");
    if (args.isSet("health-check"))
        return health(app, dir.absolutePath(), python);

    const auto diagnostic = args.value("diagnostic-profile");
    if (args.isSet("diagnostic-profile")) {
        if (!QRegularExpression("^[a-z0-9][a-z0-9-]{0,30}$").match(diagnostic).hasMatch())
            return 2;
        const auto base = qEnvironmentVariable("LOCALAPPDATA");
        if (base.isEmpty())
            return 2;
        const auto target = QDir(base).filePath("gAssets/Diagnostics/" + diagnostic);
        if (!QDir().mkpath(target))
            return 2;
        qputenv("LOCALAPPDATA", QDir::toNativeSeparators(target).toUtf8());
        app.setApplicationName("gTR-diagnostic-" + diagnostic);
    }
    const auto identity = "gAssets-gTR-" + QCryptographicHash::hash(QDir::homePath().toUtf8(), QCryptographicHash::Sha256).toHex().left(24)
        + diagnostic.toUtf8();
#ifdef Q_OS_WIN
    const auto mutexName = (QStringLiteral("Local\\gAssets.gtr.install") +
                           (diagnostic.isEmpty() ? QString{} : QStringLiteral(".diag.") + diagnostic)).toStdWString();
    const HANDLE instance = CreateMutexW(nullptr, FALSE, mutexName.c_str());
    if (instance == nullptr)
        return 3;
    const DWORD wait = WaitForSingleObject(instance, 0);
    if (wait != WAIT_OBJECT_0 && wait != WAIT_ABANDONED) {
        QLocalSocket client;
        client.connectToServer(QString::fromUtf8(identity));
        if (client.waitForConnected(700)) {
            client.write("activate");
            client.waitForBytesWritten(300);
        } else {
            QMessageBox::information(nullptr, "gTR",
                B("Программа запускается или обновляется. Повторите позже.", "Програма запускається або оновлюється. Спробуйте пізніше."));
        }
        CloseHandle(instance);
        return 0;
    }
#endif
    QLocalServer server;
    server.setSocketOptions(QLocalServer::UserAccessOption);
    if (!server.listen(QString::fromUtf8(identity))) {
#ifdef Q_OS_WIN
        ReleaseMutex(instance);
        CloseHandle(instance);
#endif
        return 3;
    }
    app.setWindowIcon(QIcon(dir.filePath("gtr/assets/gtr.ico")));
    Window window(dir.absolutePath(), python, args.value("project"));
    QObject::connect(&server, &QLocalServer::newConnection, &window, [&] {
        while (auto *client = server.nextPendingConnection()) {
            client->disconnectFromServer();
            client->deleteLater();
        }
        if (window.isMinimized())
            window.showNormal();
        window.raise();
        window.activateWindow();
        app.alert(&window, 1500);
    });
    window.show();
    const int result = app.exec();
    server.close();
#ifdef Q_OS_WIN
    ReleaseMutex(instance);
    CloseHandle(instance);
#endif
    return result;
}
