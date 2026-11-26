#include "release.hpp"
#include "translation.hpp"
#include "window.hpp"

#include <QApplication>
#include <QCommandLineParser>
#include <QDir>
#include <QFileInfo>
#include <QIcon>
#include <QImageReader>
#include <QLibraryInfo>
#include <QSettings>

#ifdef Q_OS_WIN
#define NOMINMAX
#include <windows.h>
#endif

int main(int argc, char **argv) {
#ifdef Q_OS_WIN
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_APPLICATION_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
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
    app.setWindowIcon(QIcon(dir.filePath("gtr/assets/gtr.ico")));
    Window window(dir.absolutePath(), python, args.value("project"));
    window.show();
    return app.exec();
}
