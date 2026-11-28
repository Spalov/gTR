#include "service_client.hpp"
#include "release.hpp"

#include <QJsonDocument>
#include <QProcessEnvironment>

#ifdef Q_OS_WIN
#define NOMINMAX
#include <windows.h>
#endif

#include <utility>

QByteArray requestFrame(qint64 id, const QString &method, const QJsonObject &params) {
    return QJsonDocument(QJsonObject{{"v", protocolVersion}, {"id", id}, {"method", method}, {"params", params}})
        .toJson(QJsonDocument::Compact) + '\n';
}

QJsonObject responseFrame(const QByteArray &line) {
    if (line.size() > frameLimit || line.isEmpty())
        return {};
    QJsonParseError error;
    const auto parsed = QJsonDocument::fromJson(line, &error);
    if (error.error != QJsonParseError::NoError || !parsed.isObject())
        return {};
    const auto value = parsed.object();
    if (!value["v"].isDouble() || value["v"].toInteger() != protocolVersion
        || !value["id"].isDouble() || value["id"].toInteger() <= 0)
        return {};
    const bool result = value["result"].isObject();
    const bool failure = value["error"].isObject() && value["error"].toObject()["code"].isString();
    const bool progress = value["event"].toString() == "progress" && value["stage"].isString()
        && value["done"].isDouble() && value["total"].isDouble();
    return static_cast<int>(result) + static_cast<int>(failure) + static_cast<int>(progress) == 1 ? value : QJsonObject{};
}

ServiceClient::ServiceClient(QString root, QString python, QObject *parent)
    : QObject(parent), root_(std::move(root)), python_(std::move(python)) {
    startup_.setSingleShot(true);
    connect(&startup_, &QTimer::timeout, this, [this] { fail("HANDSHAKE_TIMEOUT"); });
    inactivity_.setSingleShot(true);
    inactivity_.setInterval(180000);
    connect(&inactivity_, &QTimer::timeout, this, [this] { fail("IPC_TIMEOUT"); });
    connect(&process_, &QProcess::started, this, [this] {
        hello_ = write("hello", {});
        if (hello_ == 0)
            fail("PIPE_ERROR");
    });
    connect(&process_, &QProcess::readyReadStandardOutput, this, [this] { read(); });
    connect(&process_, &QProcess::readyReadStandardError, this, [this] { process_.readAllStandardError(); });
    connect(&process_, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (!stopping_)
            fail(error == QProcess::FailedToStart ? "START_FAILED" : "PIPE_ERROR");
    });
    connect(&process_, &QProcess::finished, this, [this](int, QProcess::ExitStatus) {
        if (!stopping_)
            fail("PROCESS_EXITED");
    });
#ifdef Q_OS_WIN
    process_.setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *args) {
        args->flags |= CREATE_NO_WINDOW;
    });
#endif
}

void ServiceClient::start() {
    if (process_.state() != QProcess::NotRunning)
        return;
    stopping_ = false;
    ready_ = false;
    hello_ = active_ = cancel_ = 0;
    sequence_ = 0;
    buffer_.clear();
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    for (const auto &key : environment.keys()) {
        if (key.startsWith("PYTHON", Qt::CaseInsensitive))
            environment.remove(key);
    }
    environment.insert("PYTHONNOUSERSITE", "1");
    environment.insert("PYTHONDONTWRITEBYTECODE", "1");
    process_.setProcessEnvironment(environment);
    process_.setWorkingDirectory(root_);
    startup_.start(12000);
    process_.start(python_, {"-X", "utf8", "-u", "-m", "gtr.ipc"});
}

void ServiceClient::stop() {
    stopping_ = true;
    startup_.stop();
    inactivity_.stop();
    if (process_.state() != QProcess::NotRunning) {
        process_.closeWriteChannel();
        if (!process_.waitForFinished(3500)) {
            process_.kill();
            process_.waitForFinished(1000);
        }
    }
}

bool ServiceClient::available() const {
    return ready_ && process_.state() == QProcess::Running;
}

qint64 ServiceClient::write(const QString &method, const QJsonObject &params) {
    const auto packet = requestFrame(++sequence_, method, params);
    return packet.size() <= frameLimit && process_.write(packet) == packet.size() ? sequence_ : 0;
}

void ServiceClient::send(const QString &method, const QJsonObject &params, Complete complete) {
    if (!available() || active_ != 0) {
        complete({}, active_ != 0 ? "BUSY" : "START_FAILED");
        return;
    }
    active_ = write(method, params);
    if (!active_) {
        complete({}, "PIPE_ERROR");
        return;
    }
    complete_ = std::move(complete);
    inactivity_.start();
}

void ServiceClient::cancel() {
    if (active_ && !cancel_)
        cancel_ = write("cancel", {{"target", active_}});
}

void ServiceClient::read() {
    const auto chunk = process_.readAllStandardOutput();
    if (stopping_)
        return;
    qsizetype first = 0;
    while (first < chunk.size()) {
        const auto end = chunk.indexOf('\n', first);
        const auto length = (end < 0 ? chunk.size() : end) - first;
        if (buffer_.size() + length > frameLimit) {
            fail("PIPE_ERROR");
            return;
        }
        buffer_.append(chunk.constData() + first, length);
        if (end < 0)
            return;
        const auto line = std::exchange(buffer_, {});
        consume(line);
        if (stopping_)
            return;
        first = end + 1;
    }
}

void ServiceClient::consume(const QByteArray &line) {
    const auto message = responseFrame(line);
    const auto id = message["id"].toInteger();
    if (message.isEmpty() || (id != hello_ && id != cancel_ && id != active_)) {
        fail("PIPE_ERROR");
        return;
    }
    const auto result = message["result"].toObject();
    const auto failure = message["error"].toObject();
    const auto error = failure["code"].toString();
    if (id == hello_) {
        startup_.stop();
        hello_ = 0;
        if (result["protocol"].toInteger() != protocolVersion || result["release"].toString() != GTR_RELEASE_LABEL) {
            fail("VERSION_MISMATCH");
            return;
        }
        ready_ = true;
        if (onReady)
            onReady();
    } else if (id == cancel_) {
        cancel_ = 0;
    } else if (message.contains("event")) {
        inactivity_.start();
        if (onProgress)
            onProgress(message["stage"].toString(), message["done"].toInteger(), message["total"].toInteger());
    } else {
        active_ = 0;
        inactivity_.stop();
        auto done = std::exchange(complete_, {});
        if (done)
            done(error.isEmpty() ? result : failure, error);
    }
}

void ServiceClient::fail(const QString &reason) {
    if (stopping_)
        return;
    stopping_ = true;
    ready_ = false;
    startup_.stop();
    inactivity_.stop();
    if (process_.state() != QProcess::NotRunning)
        process_.kill();
    active_ = 0;
    auto done = std::exchange(complete_, {});
    if (done)
        done({}, reason);
    if (onFailure)
        onFailure(reason);
}
